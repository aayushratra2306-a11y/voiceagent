"""Task 4.5 — the concurrency cap must not become a single point of failure.

Found 2026-09-12, while deciding whether to set REDIS_URL on the deployed
VM. Everything Phase 4 put behind Redis is dormant there (blank by
default), and turning it on looked like a free upgrade: the same cap, just
counted somewhere every replica can see.

It was not free. `_RedisCapacity.try_acquire` awaits Redis directly, and
`connect()` does not guard the call:

    slot_token = await try_acquire_call_slot()

so a Redis that is restarting, briefly unreachable, or simply out of
connections raised straight through the handler — a 500, and the call never
happened. With REDIS_URL blank that outcome is impossible, because the
counter is a set in this process's own memory. Switching it on would have
traded a cap that is *correct across replicas the project does not yet run*
for *every call failing whenever one container blinks*, which is the wrong
way round on a single VM.

The interesting part is what the right failure is, because the obvious two
are both wrong:

  - Failing CLOSED (refuse calls while the counter is unreachable) turns a
    Redis restart into a total outage for new callers.
  - Failing OPEN (allow everything, uncapped) is worse than it sounds. This
    cap is a MEMORY figure, not a throughput one — each live call is its own
    ~300MB process on a 4GB VM. Uncapped for even a minute means the box
    runs out of memory, which does not just refuse new calls, it kills the
    ones already in progress and the API with them.

So neither. When Redis cannot be reached the counter falls back to the
in-process one it would have been using anyway had REDIS_URL never been
set. On this deployment — one API replica — that is not a degradation at
all, it is exactly the correct number. On several replicas it is a per-
replica approximation, which is still a real ceiling and still far better
than either alternative.

Worth stating the accepted limit: slots acquired through Redis before an
outage are not known to the fallback, so the count restarts from the calls
made after the switchover. It is bounded, it recovers, and the alternative
was an OOM.
"""

import pytest

from app.core import call_capacity

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _DeadRedis:
    """A Redis whose server went away mid-life. Every operation raises the
    error redis-py actually raises, not a bare Exception."""

    def __init__(self):
        self.calls = 0

    def register_script(self, src):
        async def _script(keys=None, args=None):
            self.calls += 1
            raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

        return _script

    async def time(self):
        self.calls += 1
        raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    async def zrem(self, *a):
        raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    async def zremrangebyscore(self, *a):
        raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    async def zcard(self, *a):
        raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    async def zrange(self, *a):
        raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")


class _FlakyRedis(_DeadRedis):
    """Down, then back. `alive` flips it."""

    def __init__(self):
        super().__init__()
        self.alive = False
        self._members: dict[str, float] = {}

    def register_script(self, src):
        async def _script(keys=None, args=None):
            if not self.alive:
                raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")
            self._members[args[3]] = float(args[1])
            return 1

        return _script

    async def time(self):
        if not self.alive:
            raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")
        return (1_757_000_000, 0)

    async def zremrangebyscore(self, *a):
        if not self.alive:
            raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")
        return 0

    async def zcard(self, *a):
        if not self.alive:
            raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")
        return len(self._members)


@pytest.fixture
def dead():
    return call_capacity._RedisCapacity(_DeadRedis())


# --- the live failure ---------------------------------------------------------

async def test_a_call_is_not_refused_because_the_counter_is_unreachable(dead):
    """The exact 500. connect() does not guard this call, so anything that
    raises here is a caller who could not get through for a reason that had
    nothing to do with capacity."""
    token = await dead.try_acquire(limit=6)

    assert token is not None, "a Redis outage refused a call the system had room for"


async def test_the_cap_still_holds_while_degraded(dead):
    """Falling back must not mean falling open. The cap is a memory figure
    on a 4GB box — losing it for the duration of an outage risks an OOM
    that takes the live calls down too."""
    granted = [await dead.try_acquire(limit=2) for _ in range(4)]

    assert granted[0] is not None
    assert granted[1] is not None
    assert granted[2] is None, "the cap stopped being enforced during the outage"
    assert granted[3] is None


async def test_health_can_still_report_a_count(dead):
    """/health and /metrics both read this. An exception here takes the
    health endpoint down during exactly the incident it exists to report."""
    assert await dead.current() == 0

    await dead.try_acquire(limit=6)
    assert await dead.current() == 1


async def test_releasing_a_slot_never_raises(dead):
    """release() runs in a `finally`. Raising there replaces whatever
    exception was actually being handled, and leaks the slot besides."""
    token = await dead.try_acquire(limit=6)

    await dead.release(token)

    assert await dead.current() == 0


async def test_startup_cleanup_survives_it_too(dead):
    """Called in the lifespan. An exception here is a container that will
    not boot while Redis is down — a restart loop feeding off an outage."""
    assert await dead.release_stale_for_this_node() == 0


# --- it has to be visible, and it has to recover ------------------------------

async def test_the_outage_is_said_once_not_once_per_call(dead):
    """Loud enough to find, quiet enough to read. One line per call at
    six calls a minute buries the log in the middle of an incident."""
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING")
    try:
        for _ in range(5):
            await dead.try_acquire(limit=6)
    finally:
        logger.remove(sink)

    complaints = [line for line in lines if "CAPACITY" in line]
    assert len(complaints) == 1, (
        f"logged {len(complaints)} times for one outage — an incident should not "
        f"have to be read past"
    )


async def test_it_goes_back_to_redis_once_redis_comes_back():
    """A fallback that never un-falls-back is just a slower outage: the
    count would stay per-replica forever after one blip, silently."""
    flaky = _FlakyRedis()
    capacity = call_capacity._RedisCapacity(flaky)

    await capacity.try_acquire(limit=6)  # degrades
    flaky.alive = True
    await capacity.try_acquire(limit=6)  # should be served by Redis again

    assert flaky._members, "still using the fallback after Redis recovered"


async def test_recovery_is_announced(dead):
    """The counterpart to the outage line. Without it the log says the
    system broke and never says it came back."""
    from loguru import logger

    flaky = _FlakyRedis()
    capacity = call_capacity._RedisCapacity(flaky)
    await capacity.try_acquire(limit=6)

    lines: list[str] = []
    sink = logger.add(lines.append, level="INFO")
    try:
        flaky.alive = True
        await capacity.try_acquire(limit=6)
    finally:
        logger.remove(sink)

    assert any("CAPACITY" in line for line in lines), "recovery happened silently"
