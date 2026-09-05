"""Task 4.7 — health checks that verify something, and the watchdog that
acts on repeated failure.

The manual's own warning, stated as a test rather than a comment: an
endpoint that always returns OK would pass every test that only checks the
response exists. These check that a REAL failure (the database
unreachable) is what the report actually reflects, and that the watchdog
restarts only after several of those in a row — never on one blip, never
without ever restarting at all.
"""

from pathlib import Path

import pytest

from app.core import breaker, health

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _isolated_breaker_store(tmp_path: Path):
    breaker.use_database(tmp_path / "breakers.db")
    yield


async def test_a_healthy_system_reports_healthy():
    result = await health.report()
    assert result["healthy"] is True
    assert result["database"]["ok"] is True


async def test_an_unreachable_database_is_actually_detected(monkeypatch):
    """The exact failure the manual warns about: this must come from a real
    check, not a constant. Simulated by making the ping itself fail."""
    from app.db import mongo

    class _BrokenAdmin:
        async def command(self, *a, **k):
            raise TimeoutError("server selection timed out")

    class _BrokenClient:
        admin = _BrokenAdmin()

    monkeypatch.setattr(mongo, "client", _BrokenClient())

    ok, detail = await health.check_database()
    assert ok is False
    assert "timed out" in detail.lower() or "timeout" in detail.lower()

    result = await health.report()
    assert result["healthy"] is False
    assert result["database"]["ok"] is False


async def test_an_empty_warm_pool_is_reported_but_does_not_fail_health(monkeypatch):
    """The pool is a latency optimisation (task 2.4's cold-spawn fallback
    still serves the call), not a correctness requirement. Reporting it
    empty is the point — silently degraded latency is exactly what task
    4.7's own warning is about — but it must not turn a healthy server
    unhealthy over something that isn't actually broken."""
    from app.api import connect as connect_module

    monkeypatch.setattr(connect_module, "_idle_pool", [])
    monkeypatch.setattr(connect_module.settings, "call_worker_pool_min", 2)

    ok, detail = health.check_worker_pool()
    assert ok is True
    assert "empty" in detail.lower()

    result = await health.report()
    assert result["healthy"] is True


async def test_the_report_surfaces_a_tripped_circuit_breaker():
    """"You cannot fix what you cannot see" — a tripped breaker belongs on
    this endpoint, not only in the logs."""
    cfg = breaker.BreakerConfig(failure_threshold=1, cooldown_seconds=30)
    breaker.configure("tool:acme.test", cfg)
    breaker.record_failure("tool:acme.test", "connection refused")

    result = await health.report()

    assert result["circuit_breakers"]["tool:acme.test"]["state"] == "open"


async def test_a_single_bad_reading_does_not_trigger_a_restart():
    """The whole reason "consecutive" matters: a database blip that clears
    on its own is normal, and Motor's own connection pool already recovers
    from it. Restarting the process over one bad reading would turn brief
    flakiness into a dropped call every single time it happened."""
    restarts = []
    wd = health.Watchdog(failure_threshold=3, on_unhealthy=lambda: restarts.append(1))
    wd.last_report = {"healthy": False}  # pretend one failure already happened
    wd.consecutive_failures = 1

    await wd.check_once()  # a second consecutive failure (healthy system, forced count)

    assert restarts == []


async def test_enough_consecutive_failures_triggers_a_restart(monkeypatch):
    from app.db import mongo

    class _BrokenAdmin:
        async def command(self, *a, **k):
            raise ConnectionError("refused")

    class _BrokenClient:
        admin = _BrokenAdmin()

    monkeypatch.setattr(mongo, "client", _BrokenClient())

    restarts = []
    wd = health.Watchdog(failure_threshold=3, on_unhealthy=lambda: restarts.append(1))

    await wd.check_once()
    assert restarts == [], "restarted after only 1 failure"
    await wd.check_once()
    assert restarts == [], "restarted after only 2 failures"
    await wd.check_once()
    assert restarts == [1], "did not restart after 3 consecutive failures"


async def test_recovery_resets_the_failure_count(monkeypatch):
    """A blip, then health, then a blip again must count as 1-then-1, never
    2 — otherwise an intermittently flaky dependency would eventually
    restart the process even though it is never down for more than a
    moment at a time."""
    from app.db import mongo
    from app.db.mongo import client as real_client

    class _BrokenAdmin:
        async def command(self, *a, **k):
            raise ConnectionError("refused")

    class _BrokenClient:
        admin = _BrokenAdmin()

    restarts = []
    wd = health.Watchdog(failure_threshold=3, on_unhealthy=lambda: restarts.append(1))

    monkeypatch.setattr(mongo, "client", _BrokenClient())
    await wd.check_once()
    await wd.check_once()
    assert wd.consecutive_failures == 2

    monkeypatch.setattr(mongo, "client", real_client)
    await wd.check_once()
    assert wd.consecutive_failures == 0

    monkeypatch.setattr(mongo, "client", _BrokenClient())
    await wd.check_once()
    await wd.check_once()
    assert restarts == [], "counted a recovered blip toward the next streak"


async def test_it_does_not_hang_up_on_live_callers_over_a_database_outage(monkeypatch):
    """Found on a second read of Phase 4, and it would have been a nasty
    one to diagnose from the outside: a caller mid-sentence being cut off
    because a database they never touch went quiet.

    A live call's audio path is Deepgram -> Groq -> Cartesia end to end.
    Mongo being unreachable breaks saving the transcript, not the
    conversation. Restarting the process to fix that would drop every call
    in progress — trading three real conversations for a database write.
    """
    from app.core import call_capacity
    from app.db import mongo

    class _BrokenAdmin:
        async def command(self, *a, **k):
            raise ConnectionError("refused")

    class _BrokenClient:
        admin = _BrokenAdmin()

    monkeypatch.setattr(mongo, "client", _BrokenClient())

    class _BusyCapacity:
        async def try_acquire(self, limit):
            return "token"

        async def release(self, token):
            pass

        async def current(self):
            return 2  # two people are on a call right now

        async def release_stale_for_this_node(self):
            return 0

    call_capacity.use_backend(_BusyCapacity())
    try:
        restarts = []
        wd = health.Watchdog(
            failure_threshold=3, max_deferrals=15, on_unhealthy=lambda: restarts.append(1)
        )

        for _ in range(10):
            await wd.check_once()

        assert restarts == [], "hung up on two live callers over a database outage"
        assert wd.deferrals > 0, "the restart was never actually deferred"
    finally:
        call_capacity.use_backend(call_capacity._InProcessCapacity())


async def test_it_restarts_immediately_once_the_calls_have_finished(monkeypatch):
    """The deferral is about not dropping calls, not about never
    restarting. With nothing in progress there is nothing to protect."""
    from app.core import call_capacity
    from app.db import mongo

    class _BrokenAdmin:
        async def command(self, *a, **k):
            raise ConnectionError("refused")

    class _BrokenClient:
        admin = _BrokenAdmin()

    monkeypatch.setattr(mongo, "client", _BrokenClient())
    call_capacity.use_backend(call_capacity._InProcessCapacity())  # zero active calls

    restarts = []
    wd = health.Watchdog(failure_threshold=3, on_unhealthy=lambda: restarts.append(1))

    await wd.check_once()
    await wd.check_once()
    await wd.check_once()

    assert restarts == [1]


async def test_a_wedged_process_still_restarts_eventually(monkeypatch):
    """The bound on the wait, and why it has to exist. If the process is
    genuinely stuck, the call count it is reading may be stale and would
    never fall to zero on its own — waiting for it forever would turn the
    protection above into a server that hangs and never recovers."""
    from app.core import call_capacity
    from app.db import mongo

    class _BrokenAdmin:
        async def command(self, *a, **k):
            raise ConnectionError("refused")

    class _BrokenClient:
        admin = _BrokenAdmin()

    monkeypatch.setattr(mongo, "client", _BrokenClient())

    class _StuckCapacity:
        async def try_acquire(self, limit):
            return "token"

        async def release(self, token):
            pass

        async def current(self):
            return 1  # a count that will never come down

        async def release_stale_for_this_node(self):
            return 0

    call_capacity.use_backend(_StuckCapacity())
    try:
        restarts = []
        wd = health.Watchdog(
            failure_threshold=3, max_deferrals=3, on_unhealthy=lambda: restarts.append(1)
        )

        # Which check finally pulled the trigger. In production
        # on_unhealthy is os._exit and never returns, so there is only ever
        # one; here the callback returns, so what matters is WHEN the first
        # one happened, not how many followed.
        restarted_on_check = None
        for check in range(1, 12):
            await wd.check_once()
            if restarts and restarted_on_check is None:
                restarted_on_check = check

        assert restarted_on_check is not None, "a wedged process never recovered on its own"
        assert restarted_on_check >= wd.failure_threshold + wd.max_deferrals, (
            f"restarted on check {restarted_on_check} — that is before the deferrals "
            f"were used up, so live calls were dropped without the grace period"
        )
    finally:
        call_capacity.use_backend(call_capacity._InProcessCapacity())


async def test_recovering_clears_the_deferral_count_too(monkeypatch):
    """A deferral streak that ends in recovery must not carry over — the
    next unrelated outage should get its own full grace period."""
    from app.core import call_capacity
    from app.db import mongo
    from app.db.mongo import client as real_client

    class _BrokenAdmin:
        async def command(self, *a, **k):
            raise ConnectionError("refused")

    class _BrokenClient:
        admin = _BrokenAdmin()

    class _BusyCapacity:
        async def try_acquire(self, limit):
            return "token"

        async def release(self, token):
            pass

        async def current(self):
            return 1

        async def release_stale_for_this_node(self):
            return 0

    call_capacity.use_backend(_BusyCapacity())
    try:
        wd = health.Watchdog(failure_threshold=2, max_deferrals=5, on_unhealthy=lambda: None)

        monkeypatch.setattr(mongo, "client", _BrokenClient())
        for _ in range(4):
            await wd.check_once()
        assert wd.deferrals > 0

        monkeypatch.setattr(mongo, "client", real_client)
        await wd.check_once()

        assert wd.deferrals == 0
        assert wd.consecutive_failures == 0
    finally:
        call_capacity.use_backend(call_capacity._InProcessCapacity())


async def test_a_broken_reporting_line_never_reads_as_an_unhealthy_system():
    """The watchdog restarts the process on three unhealthy readings. A
    reporting extra throwing — a bug here, or Redis being unreachable,
    which the call count needs and a live call does not — must never be
    able to present itself as "the system is broken" and reboot a server
    that is working perfectly well."""
    from app.core import call_capacity

    class _ExplodingCapacity:
        async def try_acquire(self, limit):
            return "token"

        async def release(self, token):
            pass

        async def current(self):
            raise ConnectionError("redis is gone")

        async def release_stale_for_this_node(self):
            return 0

    call_capacity.use_backend(_ExplodingCapacity())
    try:
        result = await health.report()

        assert result["healthy"] is True, (
            "an unreadable call count was reported as an unhealthy system — "
            "three of those in a row would restart a healthy server"
        )
        assert result["capacity"]["active_calls"] is None
    finally:
        call_capacity.use_backend(call_capacity._InProcessCapacity())


async def test_an_unknown_call_count_is_not_treated_as_nobody_is_calling(monkeypatch):
    """If we cannot tell whether anyone is on a call, that is not
    permission to hang up on them."""
    from app.core import call_capacity
    from app.db import mongo

    class _BrokenAdmin:
        async def command(self, *a, **k):
            raise ConnectionError("refused")

    class _BrokenClient:
        admin = _BrokenAdmin()

    class _ExplodingCapacity:
        async def try_acquire(self, limit):
            return "token"

        async def release(self, token):
            pass

        async def current(self):
            raise ConnectionError("redis is gone")

        async def release_stale_for_this_node(self):
            return 0

    monkeypatch.setattr(mongo, "client", _BrokenClient())
    call_capacity.use_backend(_ExplodingCapacity())
    try:
        restarts = []
        wd = health.Watchdog(
            failure_threshold=2, max_deferrals=10, on_unhealthy=lambda: restarts.append(1)
        )
        for _ in range(5):
            await wd.check_once()

        assert restarts == [], "restarted while it could not tell whether calls were live"
        assert wd.deferrals > 0
    finally:
        call_capacity.use_backend(call_capacity._InProcessCapacity())


async def test_a_bug_in_the_watchdog_itself_does_not_stop_it_watching():
    """A crash inside check_once must not silently end the loop — that
    would mean the ONE THING meant to notice a stuck process quietly
    stopped noticing anything, with nothing left to say so."""
    wd = health.Watchdog(interval_seconds=0.01)

    async def _boom():
        raise RuntimeError("boom")

    wd.check_once = _boom
    task_ran_again = False

    import asyncio

    async def _run_briefly():
        nonlocal task_ran_again
        task = asyncio.create_task(wd.run_forever())
        await asyncio.sleep(0.05)
        task_ran_again = not task.done()
        task.cancel()

    await _run_briefly()
    assert task_ran_again, "run_forever() exited after the first exception"
