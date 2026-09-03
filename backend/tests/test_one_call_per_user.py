"""Guards the one-live-call-per-user rule (found live 2026-09-03).

The user reported a call as "not a proper call". The backend log showed why,
and it was not a subtle effect: two pipelines ran for one caller at the same
time. Two conversation IDs, two turn counters advancing independently
(5->6->7->8 alongside 4->5->6), the same spoken words transcribed twice
within ~50ms, two RAG lookups, two LLM completions against two private
histories, and both answers spoken into the same call. One bot said in Hindi
that it could hear them; the other said in English "I'm just sending you text
responses, so you won't actually hear a voice from me".

A stale pipeline is not harmlessly idle -- it still holds a live inbound
media track, so it keeps hearing the caller and keeps replying. Nor does it
exit promptly: the orphan in that log held an open Deepgram stream for 209
seconds and was reaped only when aiortc's own no-audio timeout fired,
minutes after the caller hung up.

The frontend fix (a synchronous re-entrancy guard on startSession) removes
the way a second connection got created in the first place. These tests
cover the server-side rule instead, which is the one that still holds when
the client is a stale tab, a reloaded page, or a network that dropped
without a close handshake -- the server should never be willing to run two
pipelines for one caller regardless of what any client does.

Deliberately unit tests against the registry rather than end-to-end calls:
the rule is about bookkeeping and process lifetime, and a real call needs
Deepgram, Cartesia, Groq and a WebRTC peer. Stub processes stand in for
workers so the assertions are about what gets terminated and what survives.
"""

import multiprocessing as mp
import time

import pytest

from app.api import connect as connect_module
from app.api.connect import _ActiveCall, _active_calls, _end_previous_calls_for


def _idle_process(ctx) -> mp.Process:
    """A real, live process standing in for a call worker."""
    proc = ctx.Process(target=time.sleep, args=(120,), daemon=True)
    proc.start()
    return proc


@pytest.fixture
def registry():
    """The registry is module-level state; leave it as we found it."""
    saved = dict(_active_calls)
    _active_calls.clear()
    yield _active_calls
    for call in _active_calls.values():
        if call.process.is_alive():
            call.process.terminate()
            call.process.join(timeout=1)
    _active_calls.clear()
    _active_calls.update(saved)


def test_a_users_previous_call_is_terminated_when_they_start_another(registry):
    ctx = mp.get_context("spawn")
    old = _idle_process(ctx)
    registry["pc-old"] = _ActiveCall(old, ctx.Queue(), user_id="user-1")

    _end_previous_calls_for("user-1")

    old.join(timeout=5)
    assert not old.is_alive(), (
        "the previous pipeline is still running — it still holds the caller's "
        "audio track, so it will keep hearing them and answering over the new call"
    )
    assert "pc-old" not in registry, "terminated call left behind in the registry"


def test_another_users_call_is_left_completely_alone(registry):
    """The rule is per-user. Two people on two calls is normal operation."""
    ctx = mp.get_context("spawn")
    mine, theirs = _idle_process(ctx), _idle_process(ctx)
    registry["pc-mine"] = _ActiveCall(mine, ctx.Queue(), user_id="user-1")
    registry["pc-theirs"] = _ActiveCall(theirs, ctx.Queue(), user_id="user-2")

    _end_previous_calls_for("user-1")

    mine.join(timeout=5)
    assert not mine.is_alive()
    assert theirs.is_alive(), "ended an unrelated user's live call"
    assert "pc-theirs" in registry


def test_every_stale_call_is_cleared_not_just_the_first(registry):
    """If two already leaked, starting a call must not leave one behind."""
    ctx = mp.get_context("spawn")
    first, second = _idle_process(ctx), _idle_process(ctx)
    registry["pc-1"] = _ActiveCall(first, ctx.Queue(), user_id="user-1")
    registry["pc-2"] = _ActiveCall(second, ctx.Queue(), user_id="user-1")

    _end_previous_calls_for("user-1")

    for proc in (first, second):
        proc.join(timeout=5)
        assert not proc.is_alive()
    assert not registry, f"stale entries survived: {list(registry)}"


def test_no_previous_call_is_a_no_op(registry):
    """The overwhelmingly common case: the first call of a session."""
    _end_previous_calls_for("user-nobody")
    assert not registry


def test_an_already_dead_call_is_reaped_without_error(registry):
    """A worker that crashed on its own must still leave the registry clean,
    and must not raise on the way out."""
    ctx = mp.get_context("spawn")
    dead = ctx.Process(target=time.sleep, args=(0,), daemon=True)
    dead.start()
    dead.join(timeout=5)
    registry["pc-dead"] = _ActiveCall(dead, ctx.Queue(), user_id="user-1")

    _end_previous_calls_for("user-1")

    assert "pc-dead" not in registry


def test_connect_ends_the_previous_call_before_claiming_a_worker():
    """Ordering matters: freeing the old pipeline first means a caller who
    restarts never briefly holds two workers, which on a 2-worker pool would
    otherwise push their own new call onto the slow cold-spawn path."""
    import inspect

    source = inspect.getsource(connect_module.connect)
    ended_at = source.find("_end_previous_calls_for")
    claimed_at = source.find("_idle_pool.pop")

    assert ended_at != -1, "connect() no longer enforces one call per user"
    assert claimed_at != -1, "worker claim not found — has connect() been restructured?"
    assert ended_at < claimed_at, (
        "connect() claims a worker before releasing the caller's previous "
        "call, so a restarting caller holds two workers at once"
    )
