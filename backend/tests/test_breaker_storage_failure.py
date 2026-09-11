"""Review finding #1 (2026-09-11) — a broken breaker store could kill a call.

The circuit breaker exists so that a sick provider fails FAST instead of
slowly. Every one of its entry points talks to SQLite, and until this fix
none of them caught anything.

That mattered because of where `allows()` is called from. The chain is:

    call_worker.start_pipeline
      -> run_voice_pipeline
        -> providers.get_stt_service / get_llm_service / get_tts_service
          -> provider_health.fallback_for
            -> breaker.allows          <- raw sqlite3, no try/except anywhere

and that whole chain runs AFTER the WebRTC SDP answer has already gone back
to the caller's browser. So a transient storage error — disk full, a
permission problem, a WAL lock timeout, a corrupted file — raised out of
`allows()`, unwound all the way up, and was swallowed by the pipeline task's
own bare `except Exception`. The caller's browser showed a connected call
with no STT, no LLM and no TTS behind it: total silence, with nothing on
/health pointing at the cause, because a failure on this path never manages
to write a breaker row either.

The rule this establishes: **the breaker is advisory, never load-bearing.**
It can refuse a call it believes is doomed, but a breaker that cannot read
its own state must get out of the way, not take the call down with it.
Failing OPEN (allowing the call) is the only safe direction — the pre-Phase-4
behaviour was no breaker at all, and degrading to that is strictly better
than dead air.
"""

import sqlite3
from pathlib import Path

import pytest

from app.core import breaker
from app.pipeline import provider_health


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path):
    breaker.use_database(tmp_path / "breakers.db")
    breaker._configs.clear()
    breaker.forget_storage_error()
    yield
    breaker._configs.clear()
    breaker.forget_storage_error()


@pytest.fixture
def broken_store(monkeypatch):
    """Every SQLite connection attempt fails, the way a full disk or a
    permission problem would."""
    def explode(*a, **k):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(breaker.sqlite3, "connect", explode)


# ------------------------------------------------------- the call path holds


def test_allows_fails_open_when_the_store_is_unreadable(broken_store):
    """The one that matters. A breaker that cannot answer must not raise into
    a live call — it must say yes and get out of the way."""
    assert breaker.allows("cartesia") is True


def test_recording_a_failure_does_not_raise(broken_store):
    """Nothing useful can be done about it, but the caller is mid-call and an
    exception here would propagate into the pipeline just the same."""
    breaker.record_failure("cartesia", "timeout")


def test_recording_a_success_does_not_raise(broken_store):
    breaker.record_success("cartesia")


def test_the_provider_factory_still_returns_a_service(broken_store):
    """One level up: this is the exact frame that used to explode. It must
    resolve to "carry on as configured" rather than raising."""
    assert provider_health.fallback_for(provider_health.TTS_CARTESIA) is None


def test_state_and_snapshot_degrade_instead_of_raising(broken_store):
    """Reporting paths too — /health/detail asking for breaker state during
    an incident must not turn into a 500."""
    assert breaker.state("cartesia") == "unknown"
    assert breaker.snapshot() == {}


# ------------------------------------------------- but it is not silent


def test_a_storage_failure_is_visible_afterwards(broken_store):
    """Failing open silently would trade dead air for an invisible loss of
    protection. The failure is recorded so /health can surface it."""
    assert breaker.storage_error() is None

    breaker.allows("cartesia")

    assert breaker.storage_error() is not None
    assert "unable to open database file" in breaker.storage_error()


def test_health_reports_the_store_as_degraded(broken_store):
    """What an operator actually sees."""
    breaker.allows("cartesia")

    assert breaker.snapshot() == {}
    assert breaker.storage_error() is not None


# ------------------------------------------------- and recovery is possible


def test_it_recovers_once_the_store_works_again(monkeypatch, tmp_path):
    """A transient error must not wedge the breaker off for the life of the
    process. The next successful call clears the flag."""
    real_connect = breaker.sqlite3.connect
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*a, **k)

    monkeypatch.setattr(breaker.sqlite3, "connect", flaky)

    assert breaker.allows("cartesia") is True
    assert breaker.storage_error() is not None

    # Second time through, the store is fine again.
    assert breaker.allows("cartesia") is True
    assert breaker.storage_error() is None


def test_a_poisoned_connection_is_discarded_not_reused(monkeypatch, tmp_path):
    """If a failure happens mid-transaction the thread's cached connection can
    be left inside an open BEGIN IMMEDIATE. Reusing it would fail every later
    call on this thread, turning one transient error into a permanent one.

    Driven through a proxy rather than by patching the connection directly:
    sqlite3.Connection.execute is read-only and cannot be monkeypatched.
    """
    breaker.use_database(tmp_path / "breakers.db")
    breaker.configure("x", breaker.BreakerConfig(failure_threshold=1, cooldown_seconds=0.0))
    breaker.record_failure("x", "boom")  # opens it, so allows() takes the write path

    real_conn = breaker._connect()
    state = {"fail_next": True}

    class FailsOnce:
        """Passes everything through, except it raises once mid-transaction."""

        def __init__(self, wrapped):
            self._wrapped = wrapped

        def execute(self, sql, *a, **k):
            if state["fail_next"] and sql.startswith("SELECT"):
                state["fail_next"] = False
                raise sqlite3.OperationalError("disk I/O error")
            return self._wrapped.execute(sql, *a, **k)

        def __getattr__(self, item):
            return getattr(self._wrapped, item)

    monkeypatch.setattr(breaker, "_connect", lambda: FailsOnce(real_conn))

    assert breaker.allows("x") is True, "should fail open, not raise"
    assert breaker.storage_error() is not None

    # Back to the real connection — which _discard_connection should have
    # dropped, so nothing is left sitting in an open transaction.
    monkeypatch.undo()
    breaker.forget_storage_error()
    breaker.allows("x")
    assert breaker.storage_error() is None, "the poisoned connection was reused"
