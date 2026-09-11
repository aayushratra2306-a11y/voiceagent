"""Review finding #2 (2026-09-11) — "no cap" also switched off the counting.

`max_concurrent_calls <= 0` is a documented, supported setting meaning "no
ceiling". Both capacity backends honoured it by returning a token WITHOUT
recording it — so the slot was never held, and `active_call_count()` reported
0 no matter how many calls were actually running.

Two things read that count, and one of them acts on it:

  - `/health` and `/metrics` report it. Wrong, but only misleading.
  - `Watchdog.check_once()` uses it to decide whether to defer a restart.

That second one is the real damage. The watchdog holds a restart back while
calls are in progress, because a live call's audio path is Deepgram/Groq/
Cartesia end to end and does not need Mongo at all — killing three real
conversations to fix a transcript write is backwards, and the deferral exists
specifically to prevent it. Reading 0 live calls skips the deferral entirely,
so with MAX_CONCURRENT_CALLS=0 and Mongo unreachable, the watchdog would hang
up on every caller — the exact outcome that logic was written to avoid.

The distinction the fix draws: "no cap" is a decision about REFUSING calls,
not about counting them. The limit check becomes conditional; the bookkeeping
always happens.
"""

import pytest

from app.core import call_capacity
from app.core.call_capacity import (
    _InProcessCapacity,
    active_call_count,
    release_call_slot,
    try_acquire_call_slot,
    use_backend,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _fresh_backend():
    use_backend(_InProcessCapacity())
    yield
    use_backend(_InProcessCapacity())


@pytest.fixture(autouse=True)
def _no_cap(monkeypatch):
    """0 is config.py's documented "no ceiling" value."""
    monkeypatch.setattr(call_capacity.settings, "max_concurrent_calls", 0)


async def test_uncapped_calls_are_still_counted():
    """The bug, stated directly. Before the fix this returned 0."""
    await try_acquire_call_slot()
    await try_acquire_call_slot()
    await try_acquire_call_slot()

    assert await active_call_count() == 3


async def test_uncapped_never_refuses():
    """The fix must not accidentally reintroduce a ceiling — "no cap" still
    means every caller gets in."""
    tokens = [await try_acquire_call_slot() for _ in range(50)]

    assert all(t is not None for t in tokens)
    assert await active_call_count() == 50


async def test_releasing_an_uncapped_slot_brings_the_count_back_down():
    """Counting without releasing would be its own bug — the number would
    climb forever and the watchdog would never restart at all."""
    token = await try_acquire_call_slot()
    assert await active_call_count() == 1

    await release_call_slot(token)

    assert await active_call_count() == 0


async def test_a_negative_limit_behaves_the_same_as_zero(monkeypatch):
    """config.py documents <= 0 as "no cap", not just 0."""
    monkeypatch.setattr(call_capacity.settings, "max_concurrent_calls", -1)

    await try_acquire_call_slot()

    assert await active_call_count() == 1


async def test_the_redis_backend_counts_uncapped_calls_too():
    """The same fix lives in the Lua script, so it needs its own test against
    a real (in-memory) Redis rather than being assumed to match."""
    import fakeredis.aioredis as fakeredis

    from app.core.call_capacity import _RedisCapacity

    use_backend(_RedisCapacity(fakeredis.FakeRedis(decode_responses=True)))
    try:
        tokens = [await try_acquire_call_slot() for _ in range(4)]

        assert all(t is not None for t in tokens), "no cap must never refuse"
        assert await active_call_count() == 4

        await release_call_slot(tokens[0])
        assert await active_call_count() == 3
    finally:
        use_backend(_InProcessCapacity())


async def test_the_watchdog_defers_a_restart_when_uncapped_calls_are_live():
    """The end-to-end consequence, through the code that actually acts on the
    count: an unhealthy reading with real calls in progress must hold the
    restart back, not hang up on three people."""
    from app.core.health import Watchdog

    await try_acquire_call_slot()
    await try_acquire_call_slot()
    await try_acquire_call_slot()

    restarted = []
    dog = Watchdog(failure_threshold=1, on_unhealthy=lambda: restarted.append(True))

    async def unhealthy_with_real_calls():
        return {
            "healthy": False,
            "capacity": {"active_calls": await active_call_count(), "limit": None},
        }

    import app.core.health as health_module

    original = health_module.report
    health_module.report = unhealthy_with_real_calls
    try:
        result = await dog.check_once()
    finally:
        health_module.report = original

    assert result["capacity"]["active_calls"] == 3, "the count the watchdog reads"
    assert restarted == [], "restarted while three calls were live"
    assert dog.deferrals == 1
