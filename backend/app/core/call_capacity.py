"""Task 4.5 — a hard ceiling on simultaneous calls.

The manual's own framing is the whole design: without a ceiling, a spike
degrades every live call at once as they all compete for the same CPU and
memory; with one, the calls already running stay perfect and anyone past the
limit gets a clean, immediate "we are busy" instead of a pipeline that starts
and then can't keep up.

The check-and-increment has to be one atomic step. Two requests that each
see 5 of 6 slots free and each decide to take the last one is exactly how a
system goes over its own limit at the worst possible moment — the two-step
version of this check is worse than not having a limit, because it creates
the illusion of one.

Two backends behind one interface, chosen the same way task 4.1 frames it:
correct for what this project actually runs today (a single API process, so
a lock in that process's own memory is genuinely atomic — no message ever
has to leave the process for the check to be correct), with the Redis path
ready for the day this project runs API replicas behind a load balancer,
at which point an in-process lock stops being enough because two replicas
would each allow up to the limit independently.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from app.core.config import settings

# Task 4.5's own tip: an atomic INCR-then-compare, via a Lua script so the
# read, the check and the write happen as a single operation on the Redis
# server — nothing else can run between them, which is the same guarantee
# asyncio.Lock gives the in-process version below.
_LUA_TRY_ACQUIRE = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= tonumber(ARGV[1]) then
    return 0
end
redis.call('INCR', KEYS[1])
return 1
"""

_KEY = "voiceagent:active_calls"


class _InProcessCapacity:
    """Correct as long as there is exactly one API process — see the module
    docstring. A plain asyncio.Lock is genuinely atomic here because nothing
    else can interleave with an `async with` block on the same event loop."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._count = 0

    async def try_acquire(self, limit: int) -> bool:
        if limit <= 0:
            return True  # 0 means "no cap", per config.py's own doc
        async with self._lock:
            if self._count >= limit:
                return False
            self._count += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            # max(0, ...): release() runs in a `finally`, and a bug or a
            # double-release must never take this negative — a negative
            # count would make the cap allow MORE calls than the limit,
            # silently defeating the entire feature.
            self._count = max(0, self._count - 1)

    async def current(self) -> int:
        return self._count


class _RedisCapacity:
    """Same contract, backed by Redis so it is correct across however many
    API replicas are running. Not used unless settings.redis_url is set."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client
        self._script = redis_client.register_script(_LUA_TRY_ACQUIRE)

    async def try_acquire(self, limit: int) -> bool:
        if limit <= 0:
            return True
        result = await self._script(keys=[_KEY], args=[limit])
        return bool(result)

    async def release(self) -> None:
        # DECR can legally go negative under a crash-then-restart race (a
        # release for a call an earlier process instance never got to
        # count); GET/compare/SET-to-0 is not worth the extra round trip
        # for a counter that self-heals as soon as active calls end.
        new_value = await self._redis.decr(_KEY)
        if new_value < 0:
            await self._redis.set(_KEY, 0)

    async def current(self) -> int:
        value = await self._redis.get(_KEY)
        return max(0, int(value or 0))


_backend = None


def _get_backend():
    global _backend
    if _backend is not None:
        return _backend

    if settings.redis_url:
        import redis.asyncio as redis

        logger.info(f"[CAPACITY] Using Redis for the call concurrency cap ({settings.redis_url})")
        _backend = _RedisCapacity(redis.from_url(settings.redis_url))
    else:
        _backend = _InProcessCapacity()
    return _backend


def use_backend(backend) -> None:
    """Tests (and a future multi-process integration test) point this at a
    fake backend directly rather than depending on settings.redis_url."""
    global _backend
    _backend = backend


async def try_acquire_call_slot() -> bool:
    """Atomically claim one of the limited call slots.

    True: a slot was claimed — the caller MUST call release_call_slot()
    exactly once when the call ends, in a `finally`, or the slot leaks.
    False: at capacity. Nothing was claimed; there is nothing to release.
    """
    return await _get_backend().try_acquire(settings.max_concurrent_calls)


async def release_call_slot() -> None:
    await _get_backend().release()


async def active_call_count() -> int:
    """For the health/metrics endpoints — how close to the ceiling this
    node currently is."""
    return await _get_backend().current()
