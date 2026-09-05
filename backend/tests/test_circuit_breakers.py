"""Task 4.6 — the circuit breaker itself.

These are about the breaker's own behaviour: when it opens, when it lets a
trial through, and what it does when a fallback exists. The tests that cover
breakers actually being WIRED into the tool caller and the provider factory
live alongside those (test_phase3_hardening.py section 11, and
test_provider_fallback.py).
"""

import time
from pathlib import Path

import pytest

from app.core import breaker


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path):
    """Every test gets its own breaker database. Without this the tests share
    one file and, worse, share it with a developer's real running server."""
    breaker.use_database(tmp_path / "breakers.db")
    breaker._configs.clear()
    yield
    breaker._configs.clear()


FAST = breaker.BreakerConfig(failure_threshold=3, window_seconds=60.0, cooldown_seconds=0.2)


def test_a_healthy_dependency_is_never_refused():
    assert breaker.allows("cartesia") is True
    breaker.record_success("cartesia")
    assert breaker.allows("cartesia") is True
    assert breaker.state("cartesia") == "closed"


def test_it_opens_only_after_the_threshold_is_reached():
    breaker.configure("cartesia", FAST)

    breaker.record_failure("cartesia", "timeout")
    assert breaker.allows("cartesia") is True, "one failure is not an outage"
    breaker.record_failure("cartesia", "timeout")
    assert breaker.allows("cartesia") is True, "two failures is still not an outage"

    breaker.record_failure("cartesia", "timeout")
    assert breaker.state("cartesia") == "open"
    assert breaker.allows("cartesia") is False


def test_an_open_breaker_refuses_instantly():
    """The whole reason this exists. A refused call must cost roughly
    nothing — if it cost anything like a timeout we would not have solved
    the problem we set out to solve."""
    breaker.configure("deepgram", FAST)
    for _ in range(3):
        breaker.record_failure("deepgram", "connect timeout")

    started = time.perf_counter()
    allowed = breaker.allows("deepgram")
    elapsed = time.perf_counter() - started

    assert allowed is False
    assert elapsed < 0.05, f"a refusal took {elapsed:.3f}s — that is not failing fast"


def test_old_failures_stop_counting():
    """Three failures spread across a month is a working provider, not a
    broken one. Without a window the breaker would trip on it."""
    breaker.configure("groq", breaker.BreakerConfig(failure_threshold=3, window_seconds=0.15))

    breaker.record_failure("groq", "500")
    breaker.record_failure("groq", "500")
    time.sleep(0.2)  # both are now outside the window
    breaker.record_failure("groq", "500")

    assert breaker.state("groq") == "closed"
    assert breaker.allows("groq") is True


def test_after_the_cooldown_exactly_one_trial_gets_through():
    """Half-open. A provider coming back from an outage should be probed by
    one call, not hit by every waiting caller at once — that is how a
    recovering service gets knocked straight back over."""
    breaker.configure("cartesia", FAST)
    for _ in range(3):
        breaker.record_failure("cartesia", "timeout")

    time.sleep(0.25)
    assert breaker.state("cartesia") == "half_open"

    assert breaker.allows("cartesia") is True, "the first caller should be the trial"
    assert breaker.allows("cartesia") is False, "a second caller must not also trial"
    assert breaker.allows("cartesia") is False


def test_a_successful_trial_closes_the_breaker():
    breaker.configure("cartesia", FAST)
    for _ in range(3):
        breaker.record_failure("cartesia", "timeout")
    time.sleep(0.25)

    assert breaker.allows("cartesia") is True
    breaker.record_success("cartesia")

    assert breaker.state("cartesia") == "closed"
    assert breaker.allows("cartesia") is True
    assert breaker.allows("cartesia") is True, "back to normal, not one-at-a-time"


def test_a_failed_trial_re_opens_immediately():
    """It does not matter that this is only the fourth failure and the
    threshold is three. The breaker asked the provider whether it had
    recovered and the answer was no."""
    breaker.configure("cartesia", FAST)
    for _ in range(3):
        breaker.record_failure("cartesia", "timeout")
    time.sleep(0.25)

    assert breaker.allows("cartesia") is True  # the trial
    breaker.record_failure("cartesia", "timeout again")

    assert breaker.state("cartesia") == "open"
    assert breaker.allows("cartesia") is False


def test_a_trial_that_never_reports_back_does_not_wedge_the_breaker_shut():
    """Task 2.4 runs calls in their own processes, and a process can be
    killed mid-call. If the trial holder simply vanishes, nothing ever calls
    record_success or record_failure — and a breaker that waits forever for
    that answer would keep a healthy provider switched off indefinitely."""
    breaker.configure("cartesia", FAST)
    for _ in range(3):
        breaker.record_failure("cartesia", "timeout")

    time.sleep(0.25)
    assert breaker.allows("cartesia") is True  # trial claimed, and then... nothing

    time.sleep(0.25)
    assert breaker.allows("cartesia") is True, "the abandoned trial should have been reclaimed"


def test_breakers_are_independent_of_each_other():
    breaker.configure("cartesia", FAST)
    breaker.configure("deepgram", FAST)
    for _ in range(3):
        breaker.record_failure("cartesia", "timeout")

    assert breaker.allows("cartesia") is False
    assert breaker.allows("deepgram") is True, "one provider's outage is not another's"


def test_one_process_trip_is_visible_to_another():
    """The reason this is not an in-memory dict. Task 2.4 gives every call
    its own interpreter, so five simultaneous calls are five processes. If
    each kept its own count, the breaker would need fifteen failures to
    trip rather than three, which is exactly the pile-up it exists to
    prevent."""
    import subprocess
    import sys

    db = breaker.DB_PATH
    breaker.configure("cartesia", FAST)
    breaker.record_failure("cartesia", "one")
    breaker.record_failure("cartesia", "two")

    # A genuinely separate interpreter, the same way call_worker spawns one.
    repo_root = Path(__file__).resolve().parents[1]
    code = (
        f"import sys; sys.path.insert(0, r'{repo_root}');"
        "from pathlib import Path;"
        "from app.core import breaker;"
        f"breaker.use_database(Path(r'{db}'));"
        "breaker.configure('cartesia', breaker.BreakerConfig(failure_threshold=3, cooldown_seconds=0.2));"
        "breaker.record_failure('cartesia', 'three');"
        "print(breaker.state('cartesia'))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert "open" in result.stdout, result.stdout
    assert breaker.state("cartesia") == "open", "this process should see the other one's trip"


def test_the_healthy_path_never_takes_a_write_lock():
    """Found on a second read of Phase 4.

    Every tool call on every live call passes through allows() and then
    record_success(), each from its own process (task 2.4). SQLite permits
    exactly ONE writer at a time — so opening a write transaction to answer
    "is anything wrong?" put a server-wide lock in front of every customer
    API request, to discover, virtually always, that nothing was. Six
    simultaneous calls would queue behind each other for it.

    Asserted by watching what actually reaches SQLite: on the healthy path
    there must be no BEGIN IMMEDIATE at all.
    """
    breaker.configure("cartesia", FAST)
    conn = breaker._connect()
    statements: list[str] = []
    # sqlite3's own tracer: Connection.execute is read-only and cannot be
    # wrapped, and this reports exactly what reached the engine anyway.
    conn.set_trace_callback(statements.append)
    try:
        assert breaker.allows("cartesia") is True
        breaker.record_success("cartesia")
    finally:
        conn.set_trace_callback(None)

    took_write_lock = [s for s in statements if "BEGIN IMMEDIATE" in s]
    assert not took_write_lock, (
        f"the healthy path opened {len(took_write_lock)} write transaction(s) — "
        f"that serialises every tool call on the server behind one lock"
    )


def test_the_half_open_trial_still_takes_the_lock_it_needs():
    """The optimisation must not go so far that two processes can both
    claim the trial — that is the stampede half-open exists to prevent, and
    it needs a real write transaction to stay atomic."""
    breaker.configure("cartesia", FAST)
    for _ in range(3):
        breaker.record_failure("cartesia", "timeout")
    time.sleep(0.25)

    conn = breaker._connect()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        assert breaker.allows("cartesia") is True  # the trial
    finally:
        conn.set_trace_callback(None)

    assert any("BEGIN IMMEDIATE" in s for s in statements), (
        "claiming the half-open trial no longer takes the write lock, so two "
        "processes could both decide they are the trial"
    )


def test_the_snapshot_reports_enough_to_act_on():
    """A tripped breaker means callers are being served by a fallback.
    Whoever is on call needs to be able to see that without reading logs."""
    breaker.configure("cartesia", FAST)
    for _ in range(3):
        breaker.record_failure("cartesia", "websocket handshake timed out")

    snap = breaker.snapshot()
    assert snap["cartesia"]["state"] == "open"
    assert snap["cartesia"]["trips"] == 1
    assert "handshake" in snap["cartesia"]["last_reason"]


def test_reading_the_snapshot_does_not_consume_the_trial():
    """Reporting must not change behaviour. A health check that quietly ate
    the half-open trial would make the breaker take twice as long to
    recover, and only on servers that are being monitored."""
    breaker.configure("cartesia", FAST)
    for _ in range(3):
        breaker.record_failure("cartesia", "timeout")
    time.sleep(0.25)

    breaker.snapshot()
    breaker.state("cartesia")
    breaker.snapshot()

    assert breaker.allows("cartesia") is True, "the trial was consumed by a health check"
