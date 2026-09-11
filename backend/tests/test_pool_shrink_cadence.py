"""Review finding #6 (2026-09-11) — the pool shrank every tick, not every
quiet window.

`next_pool_target()` shrinks by one when `quiet_ticks >=
_SHRINK_AFTER_QUIET_TICKS`, and the loop that feeds it did:

    quiet_ticks = 0 if exhausted else quiet_ticks + 1

which resets only when demand beat supply. Nothing reset it when a shrink
actually happened. So once the counter first crossed the threshold it stayed
above it forever, and the pool dropped one worker on EVERY following tick —
6 -> 5 -> 4 -> 3 -> 2 in four 15s ticks instead of one step per 60s quiet
window.

The intent (see next_pool_target's docstring) is "the pool sat unclaimed for
several checks running -> shrink by one", with the quiet window as the gap
between steps. Collapsing to the floor four times faster means a burst
arriving shortly after a lull finds a cold pool and pays the full
cold-start cost — precisely what the warm pool exists to avoid, and the
"shrink slowly to avoid thrashing" design this was meant to implement.

The counter is reset by the loop rather than by next_pool_target(), which
stays a pure function of its inputs and keeps its existing tests.
"""

import asyncio

import pytest

from app.api import connect as connect_module

_asyncio = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _quiet_pool(monkeypatch):
    """A pool with room to shrink, no demand, and plenty of memory — so the
    only thing deciding the cadence is quiet_ticks."""
    monkeypatch.setattr(connect_module, "_idle_pool", [])
    monkeypatch.setattr(connect_module, "_top_up_pool", lambda: None)
    monkeypatch.setattr(connect_module, "_shrink_pool_by_one", lambda: None)
    monkeypatch.setattr(connect_module, "_pool_exhausted_since_last_check", False)
    monkeypatch.setattr(connect_module.settings, "call_worker_pool_min", 2)
    monkeypatch.setattr(connect_module.settings, "call_worker_pool_max", 6)
    monkeypatch.setattr(connect_module.settings, "pool_min_free_memory_mb", 100)

    class _Memory:
        available = 8 * 1024 * 1024 * 1024  # plenty, so growth is never blocked

    monkeypatch.setattr(connect_module.psutil, "virtual_memory", lambda: _Memory())
    monkeypatch.setattr(connect_module, "_pool_target", 6)


async def _run_ticks(monkeypatch, how_many: int) -> list[int]:
    """Drive the real loop and record the quiet_ticks it passes in each
    time, so the cadence is observed rather than inferred."""
    seen: list[int] = []
    real = connect_module.next_pool_target
    done = asyncio.Event()

    def spy(current_target, exhausted, quiet_ticks, *a, **k):
        seen.append(quiet_ticks)
        if len(seen) >= how_many:
            done.set()
        return real(current_target, exhausted, quiet_ticks, *a, **k)

    monkeypatch.setattr(connect_module, "next_pool_target", spy)

    task = asyncio.create_task(connect_module.maintain_worker_pool_loop(interval_seconds=0))
    try:
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return seen


@_asyncio
async def test_quiet_ticks_resets_after_a_shrink(monkeypatch):
    """The bug, observed directly. Before the fix the sequence climbed
    1,2,3,4,5,6,7... and never came back down."""
    seen = await _run_ticks(monkeypatch, 12)

    threshold = connect_module._SHRINK_AFTER_QUIET_TICKS
    assert max(seen) <= threshold, (
        f"quiet_ticks reached {max(seen)} — it never reset after a shrink, so "
        f"every tick past {threshold} shrinks the pool again"
    )


@_asyncio
async def test_the_pool_does_not_collapse_to_the_floor_in_consecutive_ticks(monkeypatch):
    """The consequence that is actually felt: 6 -> 2 should take four quiet
    WINDOWS, not four ticks."""
    threshold = connect_module._SHRINK_AFTER_QUIET_TICKS

    # Just enough ticks for exactly one shrink to be due.
    await _run_ticks(monkeypatch, threshold + 1)

    assert connect_module._pool_target == 5, (
        f"target is {connect_module._pool_target} after one quiet window; "
        f"expected exactly one step down from 6"
    )


@_asyncio
async def test_it_still_shrinks_eventually(monkeypatch):
    """The fix must not stop shrinking altogether — an idle pool still has to
    give its workers back, just at the intended pace."""
    threshold = connect_module._SHRINK_AFTER_QUIET_TICKS

    await _run_ticks(monkeypatch, (threshold + 1) * 2 + 1)

    assert connect_module._pool_target == 4, (
        f"target is {connect_module._pool_target}; expected two steps down "
        f"from 6 after two quiet windows"
    )


@_asyncio
async def test_it_never_shrinks_below_the_configured_floor(monkeypatch):
    """pool_min exists so a quiet server never pays the full cold start on
    every call. Resetting the counter must not disturb that clamp."""
    await _run_ticks(monkeypatch, 40)

    assert connect_module._pool_target == 2, "the floor was breached"
