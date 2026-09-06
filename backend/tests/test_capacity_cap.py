"""Task 4.5 — the hard ceiling on simultaneous calls.

Two layers, each with its own tests. `call_capacity.py`'s own logic (atomic
acquire, release never going negative), and `connect()`'s actual wiring of
it — that a refusal is a clean, immediate response rather than a pipeline
that starts and then can't keep up, and that no path through connect()
leaks a slot it acquired.
"""

import asyncio

import pytest
from fastapi import HTTPException

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
def _cap(monkeypatch):
    monkeypatch.setattr(call_capacity.settings, "max_concurrent_calls", 3)


# ---------------------------------------------------------------------------
# call_capacity.py itself
# ---------------------------------------------------------------------------


async def test_slots_are_available_up_to_the_limit():
    tokens = [await try_acquire_call_slot() for _ in range(3)]
    assert all(t is not None for t in tokens)
    assert len(set(tokens)) == 3, "two calls were handed the same slot token"
    assert await active_call_count() == 3


async def test_the_next_call_past_the_limit_is_refused():
    for _ in range(3):
        await try_acquire_call_slot()

    assert await try_acquire_call_slot() is None
    assert await active_call_count() == 3, "a refused acquire must not still count"


async def test_a_released_slot_can_be_reused():
    tokens = [await try_acquire_call_slot() for _ in range(3)]
    assert await try_acquire_call_slot() is None

    await release_call_slot(tokens[0])

    assert await try_acquire_call_slot() is not None


async def test_releasing_the_same_slot_twice_frees_exactly_one():
    """A double release (a bug elsewhere, or a call ending twice) must not
    hand back capacity that was never taken — with a bare counter that
    would go negative and let MORE calls through than the configured
    limit, silently defeating the cap. Named slots make it idempotent."""
    tokens = [await try_acquire_call_slot() for _ in range(3)]

    await release_call_slot(tokens[0])
    await release_call_slot(tokens[0])
    await release_call_slot(tokens[0])

    assert await active_call_count() == 2, "a repeated release freed slots twice"


async def test_releasing_nothing_is_a_safe_no_op():
    """Callers release in a `finally` without first working out whether
    they hold anything."""
    await release_call_slot(None)
    assert await active_call_count() == 0


async def test_a_zero_limit_means_no_cap(monkeypatch):
    monkeypatch.setattr(call_capacity.settings, "max_concurrent_calls", 0)
    for _ in range(50):
        assert await try_acquire_call_slot() is not None


async def test_concurrent_acquires_never_exceed_the_limit():
    """The actual atomicity claim. Fired all at once so any check-then-set
    race would show up as more than 3 successes."""
    results = await asyncio.gather(*[try_acquire_call_slot() for _ in range(20)])

    granted = [t for t in results if t is not None]
    assert len(granted) == 3, f"expected exactly 3 slots granted, got {len(granted)}"
    assert len(set(granted)) == 3, "the same slot was granted to two callers"
    assert await active_call_count() == 3


# ---------------------------------------------------------------------------
# connect()'s wiring
# ---------------------------------------------------------------------------


async def test_connect_refuses_with_a_clean_response_when_full(monkeypatch):
    """The point of the whole task: existing calls stay untouched and a new
    one gets an immediate, clear answer rather than starting and then
    degrading."""
    from app.api import connect as connect_module

    for _ in range(3):
        await try_acquire_call_slot()

    async def _fake_owned_bot(*a, **k):
        raise AssertionError("should never get this far once the cap is hit")

    monkeypatch.setattr(connect_module, "fetch_owned_bot", _fake_owned_bot)

    class _FakeUser:
        id = "user-x"

    with pytest.raises(HTTPException) as excinfo:
        # fetch_owned_bot is patched to blow up if reached; the capacity
        # check in the real connect() runs before it, so this call must
        # never reach that line at all — reordering connect() would surface
        # here as the AssertionError instead of the 503 below.
        body = connect_module.WebRTCOffer(bot_id="bot-1", sdp="x", type="offer")
        await connect_module.connect(body, current_user=_FakeUser())

    assert excinfo.value.status_code == 503


async def test_connect_checks_capacity_after_freeing_the_callers_own_stale_call():
    """A user reconnecting must never be blocked by their OWN previous call
    still holding a slot — ending it happens first, specifically so the
    capacity check that follows sees the freed slot."""
    import inspect

    from app.api import connect as connect_module

    source = inspect.getsource(connect_module.connect)
    ended_at = source.find("_end_previous_calls_for")
    checked_at = source.find("try_acquire_call_slot")

    assert ended_at != -1 and checked_at != -1
    assert ended_at < checked_at, (
        "connect() checks capacity before releasing the caller's own stale "
        "call, so reconnecting could be refused by a slot the caller "
        "themselves is about to free"
    )


async def test_capacity_is_checked_before_the_database_lookup():
    """A system already full should not spend a database round trip
    discovering that — the cap is meant to fail fast."""
    import inspect

    from app.api import connect as connect_module

    source = inspect.getsource(connect_module.connect)
    checked_at = source.find("try_acquire_call_slot")
    fetched_at = source.find("fetch_owned_bot")

    assert checked_at != -1 and fetched_at != -1
    assert checked_at < fetched_at, (
        "connect() looks up the bot before checking capacity — a full "
        "system now pays a database round trip on every refused call"
    )


async def test_an_unowned_bot_id_releases_the_slot_it_acquired(monkeypatch):
    """The slot is claimed before the bot lookup (to fail fast on capacity),
    which means a bad bot_id after that point must give it back — otherwise
    every mistyped or unauthorized bot_id would leak a slot."""
    from app.api import connect as connect_module

    monkeypatch.setattr(connect_module, "_end_previous_calls_for", _noop_async)

    async def _raises(*a, **k):
        raise HTTPException(status_code=404, detail="bot not found")

    monkeypatch.setattr(connect_module, "fetch_owned_bot", _raises)

    class _FakeUser:
        id = "user-z"

    before = await active_call_count()
    body = connect_module.WebRTCOffer(bot_id="does-not-exist", sdp="x", type="offer")

    with pytest.raises(HTTPException) as excinfo:
        await connect_module.connect(body, current_user=_FakeUser())

    assert excinfo.value.status_code == 404
    assert await active_call_count() == before, "the acquired slot was never released"


async def test_a_setup_timeout_releases_the_slot_it_acquired(monkeypatch):
    """Found by reading, not by symptom: acquiring a slot and then failing
    before the call is registered would otherwise leak it forever — nothing
    else in the system knows a call that never got an answer ever existed."""
    from app.api import connect as connect_module

    monkeypatch.setattr(connect_module, "_idle_pool", [])
    monkeypatch.setattr(connect_module, "_end_previous_calls_for", _noop_async)

    class _NeverAnswers:
        def get(self, timeout):
            raise TimeoutError("simulated setup timeout")

        def put(self, *a, **k):
            pass

    class _DummyProc:
        def start(self):
            pass

        def terminate(self):
            pass

    monkeypatch.setattr(connect_module._MP, "Queue", lambda: _NeverAnswers())
    monkeypatch.setattr(connect_module._MP, "Process", lambda **k: _DummyProc())

    async def _fake_owned_bot(*a, **k):
        class _Bot:
            id = "bot-1"
            name = "test"
            system_prompt = ""
            voice_id = "v"
            llm_model = "m"
            language = "en"
            user_id = "owner-1"
            redact_transcripts = []

        return _Bot()

    monkeypatch.setattr(connect_module, "fetch_owned_bot", _fake_owned_bot)

    class _FakeUser:
        id = "user-y"

    before = await active_call_count()
    body = connect_module.WebRTCOffer(bot_id="bot-1", sdp="x", type="offer")

    with pytest.raises(HTTPException) as excinfo:
        await connect_module.connect(body, current_user=_FakeUser())

    assert excinfo.value.status_code == 504
    assert await active_call_count() == before, "the acquired slot was never released"


# ---------------------------------------------------------------------------
# Surviving a restart (the Redis backend)
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_capacity(monkeypatch):
    """The real _RedisCapacity against an in-memory Redis, so the Lua
    script, the sorted set and the sweep are all genuinely exercised
    rather than mocked into agreeing with me."""
    import fakeredis.aioredis as fakeredis

    from app.core.call_capacity import _RedisCapacity

    backend = _RedisCapacity(fakeredis.FakeRedis(decode_responses=True))
    use_backend(backend)
    yield backend
    use_backend(_InProcessCapacity())


async def test_the_redis_backend_enforces_the_same_limit(redis_capacity):
    tokens = [await try_acquire_call_slot() for _ in range(3)]
    assert all(t is not None for t in tokens)
    assert await try_acquire_call_slot() is None
    assert await active_call_count() == 3


async def test_a_restart_does_not_permanently_cost_capacity(redis_capacity, monkeypatch):
    """The defect a second read of Phase 4 turned up, and it would have
    been a slow-acting disaster.

    Task 4.7's watchdog restarts this process ON PURPOSE when a dependency
    stays broken. The in-memory registry of live calls comes back empty, so
    nothing would ever release the slots those calls were holding. With a
    bare INCR/DECR counter in Redis, every restart permanently burned
    however many calls were live — and after enough of them the server
    would refuse every caller while running none, with no way back except
    someone noticing and clearing the key by hand.
    """
    for _ in range(3):
        await try_acquire_call_slot()
    assert await active_call_count() == 3

    # The process dies and comes back: same node, brand new (empty) call
    # registry, nothing left that knows those tokens.
    await call_capacity.release_slots_from_a_previous_life()

    assert await active_call_count() == 0, "the restart stranded its own slots"
    assert await try_acquire_call_slot() is not None


async def test_a_restart_never_touches_another_replicas_live_calls(redis_capacity, monkeypatch):
    """The cleanup is safe precisely because it is scoped to this node.
    A second replica restarting must not free the slots of calls that are
    still genuinely in progress somewhere else."""
    # Scored NOW — this stands for a call that is genuinely in progress on
    # another replica right this second, not an old leftover. (Dating it in
    # the past would just prove the TTL sweep works, which is the test
    # below, not this one.)
    now_seconds, _micros = await redis_capacity._redis.time()
    other_node_token = "othernode1234:aaaaaaaaaaaaaaaa"
    await redis_capacity._redis.zadd(
        "voiceagent:active_calls", {other_node_token: float(now_seconds)}
    )

    await try_acquire_call_slot()  # one of ours
    await call_capacity.release_slots_from_a_previous_life()

    remaining = await redis_capacity._redis.zrange("voiceagent:active_calls", 0, -1)
    assert other_node_token in remaining, "freed another replica's live call"
    assert await active_call_count() == 1, "the other replica's call stopped being counted"


async def test_an_unreachable_redis_at_startup_does_not_stop_the_server_booting():
    """This cleanup runs in the lifespan. With REDIS_URL set but Redis not
    yet answering — a compose restart bringing containers up in whatever
    order, a brief blip — an exception here would take the whole API down
    at boot over stale bookkeeping. The TTL sweep reclaims those slots
    regardless; this is only the fast path."""

    class _UnreachableRedis:
        async def try_acquire(self, limit):
            raise ConnectionError("redis is not up yet")

        async def release(self, token):
            pass

        async def current(self):
            raise ConnectionError("redis is not up yet")

        async def release_stale_for_this_node(self):
            raise ConnectionError("redis is not up yet")

    use_backend(_UnreachableRedis())
    try:
        await call_capacity.release_slots_from_a_previous_life()  # must not raise
    finally:
        use_backend(_InProcessCapacity())


async def test_a_slot_older_than_any_possible_call_is_swept(redis_capacity):
    """The second, independent recovery path: a node that never comes back
    at all still has its slots reclaimed, because nothing can legitimately
    hold one for longer than a call can last."""
    ancient = "deadnode0001:bbbbbbbbbbbbbbbb"
    await redis_capacity._redis.zadd("voiceagent:active_calls", {ancient: 1.0})  # 1970

    assert await active_call_count() == 0, "a slot from a long-dead node was still counted"


async def test_a_slot_within_its_lifetime_is_not_swept(redis_capacity):
    """The sweep must not go so far that it frees calls that are genuinely
    still running — two workers on one slot is the thing the cap exists to
    prevent."""
    now_seconds, _micros = await redis_capacity._redis.time()
    recent = "othernode9999:cccccccccccccccc"
    await redis_capacity._redis.zadd("voiceagent:active_calls", {recent: float(now_seconds) - 60})

    assert await active_call_count() == 1


async def _noop_async(*a, **k):
    pass
