"""Task 4.7 — health checks that verify something, and a watchdog that acts
on what they find.

The manual's own warning is specific: "make the health check actually test
something meaningful... an endpoint that always returns OK will happily
report a completely broken worker as healthy." Before this, /health did
exactly that — `return {"status": "ok"}`, no matter what. This file is what
makes it mean something:

  - `check_database()` actually reaches MongoDB, with a short timeout, so a
    server that can be pinged but cannot reach its own database is reported
    as broken rather than fine.
  - `check_worker_pool()` looks at whether the warm pool this process is
    supposed to be maintaining (task 2.4's latency work) is actually there —
    a pool silently stuck at zero means every caller pays the cold-start
    cost with nothing saying why.
  - `report()` also surfaces the circuit breakers (task 4.6) and the call
    capacity (task 4.5): not failures in themselves — a tripped breaker
    means the system is coping with an outage, which is the opposite of
    broken — but exactly the kind of thing "you cannot fix what you cannot
    see" is about, so they ride along on the same endpoint rather than
    needing a second one someone has to remember exists.

The watchdog is the "and automatic restarts" half. A stuck process does not
reliably notice its own deadlock, so this is a plain periodic timer, not
triggered by the same event loop that might be the thing that's stuck —
if THIS loop is wedged, the check never runs and the watchdog does nothing,
same as `check_worker_pool` catching a wedged pool from a live loop. What
still catches a fully wedged process is the container platform's own
HEALTHCHECK (see deploy/Dockerfile) hitting /health from outside the
process entirely and Docker restarting on repeated failure — the two are
complementary, not redundant: this watchdog restarts on a DEPENDENCY being
broken (the database is unreachable) even while the event loop is
otherwise responsive; Docker's HEALTHCHECK restarts when the process
cannot even answer that from outside.
"""

from __future__ import annotations

import asyncio
import os
import time

from loguru import logger

# Kept as instance attributes rather than plain functions so tests can hand
# the watchdog a fresh instance with its own counters instead of resetting
# module-level state between tests.


async def check_database(timeout: float = 3.0) -> tuple[bool, str]:
    """Not "is the client object constructed" — an actual round trip.
    `ping` is the standard trivial admin command for exactly this."""
    from app.db.mongo import client

    try:
        await asyncio.wait_for(client.admin.command("ping"), timeout=timeout)
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_worker_pool() -> tuple[bool, str]:
    """The warm pool existing is a latency optimization, not a correctness
    requirement (a cold spawn still serves the call — see call_worker.py),
    so this NEVER fails the overall health check on its own. It is reported
    for the same reason a slow query is worth logging even though it isn't
    down: an operator watching this endpoint should see the warm pool
    silently stuck at zero well before the first caller notices calls
    getting slower.
    """
    from app.api import connect as connect_module

    size = len(connect_module._idle_pool)
    target = connect_module.settings.call_worker_pool_min
    if target == 0:
        return True, "pooling disabled"
    if size == 0:
        return True, "pool is empty — calls are falling back to cold-spawn"
    return True, f"{size} warm worker(s)"


async def report() -> dict:
    """Everything the health endpoint and an operator glancing at it need,
    in one call. `healthy` is the single boolean a load balancer or Docker
    HEALTHCHECK should key off; everything else is context for a person."""
    from app.core import breaker
    from app.core.call_capacity import active_call_count
    from app.core.config import settings
    from app.pipeline import provider_health

    db_ok, db_detail = await check_database()
    pool_ok, pool_detail = check_worker_pool()

    return {
        "healthy": db_ok and pool_ok,
        "database": {"ok": db_ok, "detail": db_detail or "reachable"},
        "worker_pool": {"ok": pool_ok, "detail": pool_detail},
        "capacity": {
            "active_calls": await active_call_count(),
            "limit": settings.max_concurrent_calls or None,
        },
        # Every breaker this node knows about (both the per-host tool
        # breakers from task 4.6 and the provider ones), plus the provider
        # entries repeated with their backup readiness — the tool ones have
        # no backup concept, so they only ever appear in the first dict.
        "circuit_breakers": breaker.snapshot(),
        "providers": provider_health.health(),
    }


class Watchdog:
    """Runs `report()` on a timer and restarts the process after enough
    CONSECUTIVE unhealthy readings in a row.

    Consecutive, not cumulative: a database blip that clears itself a second
    later is exactly what task 2.1's retry logic and Motor's own connection
    pool already recover from without help. Restarting the whole process
    over one bad reading would turn brief, ordinary flakiness into a dropped
    call every time it happened. Requiring several IN A ROW is what turns
    this into "this is not coming back on its own."

    `on_unhealthy` defaults to a hard process exit — deliberately `os._exit`
    rather than `sys.exit` or raising: this fires from a background task
    that may itself be running alongside code in a stuck state, and the
    thing that has to be relied on to work is the one guarantee `os._exit`
    actually gives — the process ends, immediately, no cleanup handlers that
    could themselves hang. `restart: unless-stopped` in docker-compose.yml
    is what brings it back. Injectable so tests never actually kill the test
    process.
    """

    def __init__(
        self,
        interval_seconds: float = 20.0,
        failure_threshold: int = 3,
        on_unhealthy=None,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.failure_threshold = failure_threshold
        self._on_unhealthy = on_unhealthy or (lambda: os._exit(1))
        self.consecutive_failures = 0
        self.last_report: dict | None = None
        self.last_checked_at: float | None = None

    async def check_once(self) -> dict:
        result = await report()
        self.last_report = result
        self.last_checked_at = time.time()

        if result["healthy"]:
            if self.consecutive_failures:
                logger.info(
                    f"[HEALTH] Recovered after {self.consecutive_failures} "
                    f"consecutive unhealthy check(s)"
                )
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
            logger.warning(
                f"[HEALTH] Unhealthy ({self.consecutive_failures}/"
                f"{self.failure_threshold}): {result}"
            )
            if self.consecutive_failures >= self.failure_threshold:
                logger.error(
                    f"[HEALTH] {self.consecutive_failures} consecutive unhealthy checks "
                    f"— restarting this process"
                )
                self._on_unhealthy()

        return result

    async def run_forever(self) -> None:
        while True:
            try:
                await self.check_once()
            except Exception as e:
                # A bug IN the watchdog must never be the reason the process
                # restarts, or stops restarting — that would defeat its own
                # purpose in the most confusing way possible.
                logger.error(f"[HEALTH] watchdog check itself failed: {e}")
            await asyncio.sleep(self.interval_seconds)
