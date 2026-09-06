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

**Slots are held by name, and can always be recovered.** That is the part
worth reading, and the part a first pass at this got wrong.

A counter that is only ever INCR'd and DECR'd cannot survive the process
dying, and this process is *designed* to die: task 4.7's watchdog restarts
it deliberately when a dependency stays broken. With a plain counter in
Redis, every one of those restarts would strand however many calls were
live at the time — permanently, since the registry that knew about them
(`connect.py`'s `_active_calls`) is in-memory and comes back empty. After a
few restarts the count would sit at the limit forever and the server would
refuse every caller while running none. Exactly the "decrement reliably
when a call ends, INCLUDING ON CRASHES" the manual asks for, and exactly
what a naive counter cannot do.

So a slot is a named member of a Redis sorted set, scored by the time it was
taken, and there are two independent ways it comes back:

  - Every acquire first sweeps out anything older than SLOT_TTL_SECONDS.
    No call can outlive that (call_worker.py caps a call at one hour), so
    an entry older than it is definitionally dead — whoever held it is not
    coming back to release it.
  - A node clears its OWN leftovers at startup. When this process boots,
    `_active_calls` is empty by definition, so any slot still tagged with
    this node's id belongs to a call that died with the last incarnation.
    This is what makes a watchdog restart cost nothing at all rather than
    an hour of degraded capacity.

The in-process backend keeps the same token-based shape even though it
cannot leak across a restart (its state dies with the process, which is
exactly right) — one interface, so the difference between one replica and
several is a setting and not a second code path.
"""

from __future__ import annotations

import asyncio
import uuid

from loguru import logger

from app.core.config import settings

_KEY = "voiceagent:active_calls"

# Nothing can legitimately hold a slot for longer than this. call_worker.py's
# MAX_CALL_LIFETIME_SECONDS force-ends any call at one hour; the margin on
# top is for the teardown that follows. Not imported from call_worker on
# purpose — that module pulls in the whole pipecat stack, and this one is
# imported by the metrics and health endpoints.
SLOT_TTL_SECONDS = 3600 + 300

# Identifies this process's slots so it can clean up after its own previous
# incarnation without touching another replica's live calls.
NODE_ID = uuid.uuid4().hex[:12]

# Task 4.5's own tip, as a Lua script so the sweep, the count, the check and
# the write happen as ONE operation on the Redis server: nothing else can run
# between them, which is the same guarantee asyncio.Lock gives the
# in-process version below.
_LUA_TRY_ACQUIRE = """
local cutoff = tonumber(ARGV[2]) - tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
local current = redis.call('ZCARD', KEYS[1])
if current >= tonumber(ARGV[1]) then
    return 0
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
return 1
"""


def _new_token() -> str:
    """Node-tagged so a restart can find its own abandoned slots, and unique
    so two calls never collide on one member name."""
    return f"{NODE_ID}:{uuid.uuid4().hex[:16]}"


class _InProcessCapacity:
    """Correct as long as there is exactly one API process — see the module
    docstring. A plain asyncio.Lock is genuinely atomic here because nothing
    else can interleave with an `async with` block on the same event loop.

    Nothing to recover on restart: this state lives and dies with the
    process, which is the correct behaviour rather than a limitation.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._held: set[str] = set()

    async def try_acquire(self, limit: int) -> str | None:
        token = _new_token()
        if limit <= 0:
            return token  # 0 means "no cap", per config.py's own doc
        async with self._lock:
            if len(self._held) >= limit:
                return None
            self._held.add(token)
            return token

    async def release(self, token: str) -> None:
        async with self._lock:
            # discard, not remove: release() runs in a `finally`, and a
            # double release must never raise, nor take the count negative
            # — a negative count would let MORE calls through than the
            # limit, silently defeating the entire feature.
            self._held.discard(token)

    async def current(self) -> int:
        return len(self._held)

    async def release_stale_for_this_node(self) -> int:
        return 0  # nothing survives this process, so nothing to clean up


class _RedisCapacity:
    """Same contract, backed by Redis so it is correct across however many
    API replicas are running. Not used unless settings.redis_url is set."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client
        self._script = redis_client.register_script(_LUA_TRY_ACQUIRE)

    async def _now(self) -> float:
        """Redis's clock, not this process's.

        Replicas do not agree on the time, and a slot's age decides when it
        is swept. One replica running a few minutes fast would otherwise
        expire another's live calls.
        """
        seconds, microseconds = await self._redis.time()
        return float(seconds) + float(microseconds) / 1_000_000

    async def try_acquire(self, limit: int) -> str | None:
        token = _new_token()
        if limit <= 0:
            return token
        now = await self._now()
        granted = await self._script(
            keys=[_KEY], args=[limit, now, SLOT_TTL_SECONDS, token]
        )
        return token if granted else None

    async def release(self, token: str) -> None:
        await self._redis.zrem(_KEY, token)

    async def current(self) -> int:
        now = await self._now()
        await self._redis.zremrangebyscore(_KEY, "-inf", now - SLOT_TTL_SECONDS)
        return int(await self._redis.zcard(_KEY))

    async def release_stale_for_this_node(self) -> int:
        """Drop every slot tagged with THIS node id.

        Safe to call only at startup, and only then: at that moment this
        process is running no calls at all, so anything still carrying its
        node id is a leftover from the incarnation that died. Another
        replica's slots have a different id and are never touched.
        """
        members = await self._redis.zrange(_KEY, 0, -1)
        stale = [m for m in members if str(m).startswith(f"{NODE_ID}:")]
        if stale:
            await self._redis.zrem(_KEY, *stale)
        return len(stale)


_backend = None


def _get_backend():
    global _backend
    if _backend is not None:
        return _backend

    if settings.redis_url:
        import redis.asyncio as redis

        logger.info(f"[CAPACITY] Using Redis for the call concurrency cap ({settings.redis_url})")
        _backend = _RedisCapacity(redis.from_url(settings.redis_url, decode_responses=True))
    else:
        _backend = _InProcessCapacity()
    return _backend


def use_backend(backend) -> None:
    """Tests (and a future multi-process integration test) point this at a
    fake backend directly rather than depending on settings.redis_url."""
    global _backend
    _backend = backend


async def try_acquire_call_slot() -> str | None:
    """Atomically claim one of the limited call slots.

    Returns a slot TOKEN on success — the caller MUST pass it back to
    release_call_slot() exactly once when the call ends, in a `finally`, or
    the slot is held until its TTL sweeps it.
    None means at capacity: nothing was claimed, and there is nothing to
    release.
    """
    return await _get_backend().try_acquire(settings.max_concurrent_calls)


async def release_call_slot(token: str | None) -> None:
    """Give a slot back. A None token (nothing was ever acquired) is a
    deliberate no-op, so callers can release unconditionally in a `finally`
    without first working out whether they hold anything."""
    if token is None:
        return
    await _get_backend().release(token)


async def active_call_count() -> int:
    """For the health/metrics endpoints — how close to the ceiling this
    node currently is."""
    return await _get_backend().current()


async def release_slots_from_a_previous_life() -> None:
    """Called once at startup (main.py's lifespan).

    Task 4.7's watchdog restarts this process on purpose when a dependency
    stays broken, and a crash can do the same at any time. Either way the
    calls that were live are gone but their slots are not — this is what
    stops a restart quietly costing capacity until the TTL catches up an
    hour later.
    """
    try:
        released = await _get_backend().release_stale_for_this_node()
    except Exception as e:
        # Best-effort cleanup, never a reason to refuse to boot. This runs
        # in the lifespan, and with REDIS_URL set but Redis not yet
        # reachable — a compose restart bringing containers up in whatever
        # order, a brief network blip — an exception here would take the
        # whole API down at startup over some stale bookkeeping. The TTL
        # sweep reclaims those slots anyway; this is only the fast path.
        logger.warning(
            f"[CAPACITY] Could not check for slots left by a previous run "
            f"({type(e).__name__}: {e}). They will be swept by their TTL instead."
        )
        return
    if released:
        logger.warning(
            f"[CAPACITY] Released {released} call slot(s) left behind by a previous run of "
            f"this node — those calls did not survive the restart"
        )
