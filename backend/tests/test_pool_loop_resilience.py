"""Review finding #3 (2026-09-11) — one IndexError killed pool autoscaling
for the life of the process.

Two separate defects that compound into that outcome.

**The race.** `_shrink_pool_by_one()` does a check-then-pop under
`_pool_lock`. `connect()` claims a worker with a bare `_idle_pool.pop(0)`
that does NOT take that lock — and it cannot, because `_top_up_pool()` holds
the lock while calling `Process.start()`, so waiting for it on the event loop
would stall every request in the process for the length of an interpreter
spawn. So the two really do interleave: the shrink thread passes its
`len(_idle_pool) <= target` check, `connect()` empties the list, and the
shrink thread's own `.pop()` raises IndexError.

This is a genuine cross-thread race, not a theoretical one:
`_shrink_pool_by_one` runs via `run_in_executor`, i.e. on a real
ThreadPoolExecutor OS thread, concurrently with the event loop.

**The amplifier.** `maintain_worker_pool_loop` has no try/except around its
body and nothing supervises the task, so that one exception ends the loop
permanently. After it, the pool never tops up, never replaces a worker that
died, and never autoscales again — every subsequent call silently falls back
to the slow cold-spawn path with nothing in the logs saying why.

The same unsupervised shape is in `reap_dead_calls_loop`, where the cost is
`_active_calls` growing forever and finished processes never being reaped.

Fixed in both directions: the shrink tolerates an empty pool (losing the race
is a correct, expected outcome — someone else took the worker we were going
to retire, which is exactly what we wanted), and both loops survive an
unexpected error at the cost of one iteration instead of forever.
"""

import asyncio

import pytest

from app.api import connect as connect_module

# Applied per-test rather than module-wide: two of these are synchronous,
# and a blanket asyncio mark on a sync test is a pytest warning per test.
_asyncio = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _empty_pool():
    original = list(connect_module._idle_pool)
    connect_module._idle_pool.clear()
    yield
    connect_module._idle_pool[:] = original


# --------------------------------------------------------------- the race


def test_shrinking_an_already_empty_pool_does_not_raise():
    """Losing the race is a correct outcome, not an error: someone claimed
    the worker we were about to retire, which is what we wanted anyway."""
    connect_module._idle_pool.clear()
    connect_module._pool_target = 0

    connect_module._shrink_pool_by_one()  # must not raise


def test_shrinking_survives_the_pool_emptying_mid_decision(monkeypatch):
    """The actual interleaving: the length check passes, then the list is
    emptied by connect() before the pop runs."""
    class Vanishing(list):
        """Reports one worker to the length check, then is empty by the time
        anything tries to take it — exactly what connect()'s unlocked
        pop(0) does from the event-loop thread."""

        def __len__(self):
            return 1

        def pop(self, *a):
            raise IndexError("pop from empty list")

    monkeypatch.setattr(connect_module, "_idle_pool", Vanishing())
    monkeypatch.setattr(connect_module, "_pool_target", 0)

    connect_module._shrink_pool_by_one()  # must not raise


# ----------------------------------------------------------- the amplifier


@_asyncio
async def test_the_maintenance_loop_survives_an_unexpected_error(monkeypatch):
    """One bad tick must cost one tick, not the whole background task.

    Without this, the loop that tops the pool up, replaces dead workers and
    autoscales simply stops — and nothing restarts it.
    """
    calls = {"n": 0}

    def explode_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient OSError from Process.start()")

    monkeypatch.setattr(connect_module, "_top_up_pool", explode_once)
    monkeypatch.setattr(connect_module, "_pool_target", 0)

    task = asyncio.create_task(connect_module.maintain_worker_pool_loop(interval_seconds=0.01))
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert calls["n"] > 1, "the loop died on the first error instead of carrying on"


@_asyncio
async def test_the_reaper_loop_survives_an_unexpected_error(monkeypatch):
    """Same shape, same fix. If this dies, _active_calls grows forever and
    finished worker processes are never joined."""
    calls = {"n": 0}

    class Exploding(dict):
        def items(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return super().items()

    monkeypatch.setattr(connect_module, "_active_calls", Exploding())

    task = asyncio.create_task(connect_module.reap_dead_calls_loop(interval_seconds=0.01))
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert calls["n"] > 1, "the reaper died on the first error instead of carrying on"


@_asyncio
async def test_cancelling_the_loops_still_stops_them(monkeypatch):
    """The resilience must not swallow CancelledError — shutdown depends on
    these tasks actually ending when the lifespan cancels them."""
    monkeypatch.setattr(connect_module, "_top_up_pool", lambda: None)
    monkeypatch.setattr(connect_module, "_pool_target", 0)

    for coro in (
        connect_module.maintain_worker_pool_loop(interval_seconds=0.01),
        connect_module.reap_dead_calls_loop(interval_seconds=0.01),
    ):
        task = asyncio.create_task(coro)
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
