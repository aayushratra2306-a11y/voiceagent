"""Review finding #8 (2026-09-11) — a Prometheus scrape stalled live calls.

`_build_registry()` is async, and called `breaker.snapshot()` — plain,
blocking sqlite3 I/O — directly on the event loop. That is the same loop
negotiating WebRTC signalling for live calls, and `deploy/prometheus.yml`
scrapes every 15 seconds, so the stall was on a fixed schedule rather than
an unlucky one.

`health.report()` did the same thing, which matters more: the watchdog calls
it every 20s and /health serves it to Docker's HEALTHCHECK.

It is notable that connect.py in this same branch is careful to push
equivalent blocking work (`_top_up_pool`, `_shrink_pool_by_one`, the
`answer_queue.get`) into `run_in_executor`. These two paths simply missed it.

Two fixes, because there were two problems stacked:

  - **The N+1.** `snapshot()` selected every row in bulk and then called
    `state(name)` per breaker, which re-queried the same row it had just
    fetched. The bulk row already carries `opened_at`, which is all the
    state decision needs, so it is computed rather than re-read — one query
    instead of 1+N.
  - **The blocking.** `snapshot_async()` runs it in a worker thread. The
    connection is thread-local by design (see `_connect`), so a worker
    thread simply opens its own; nothing is shared across threads.
"""

import asyncio
import time
from pathlib import Path

import pytest

from app.core import breaker

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path):
    breaker.use_database(tmp_path / "breakers.db")
    breaker._configs.clear()
    breaker.forget_storage_error()
    yield
    breaker._configs.clear()
    breaker.forget_storage_error()


def _open_breakers(how_many: int) -> None:
    for i in range(how_many):
        name = f"provider-{i}"
        breaker.configure(name, breaker.BreakerConfig(failure_threshold=1))
        breaker.record_failure(name, "down")


# ------------------------------------------------------------------ the N+1


async def test_snapshot_does_not_re_query_per_breaker():
    """One SELECT for the whole snapshot, however many breakers exist.

    Before the fix this was 1 + N: a bulk select, then state() re-reading
    each row it had already fetched."""
    _open_breakers(5)

    conn = breaker._connect()
    original = conn.execute
    selects = []

    class Counting:
        def execute(self, sql, *a, **k):
            if sql.strip().upper().startswith("SELECT"):
                selects.append(sql)
            return original(sql, *a, **k)

        def __getattr__(self, item):
            return getattr(conn, item)

    import unittest.mock as mock

    with mock.patch.object(breaker, "_connect", lambda: Counting()):
        result = breaker.snapshot()

    assert len(result) == 5
    assert len(selects) == 1, (
        f"{len(selects)} SELECTs for 5 breakers — the per-breaker re-query is back"
    )


async def test_snapshot_still_reports_the_right_states():
    """The N+1 fix recomputes state from the bulk row instead of re-reading
    it, so the states themselves need checking, not just the query count."""
    breaker.configure("open-one", breaker.BreakerConfig(failure_threshold=1, cooldown_seconds=60))
    breaker.record_failure("open-one", "down")

    breaker.configure("half-one", breaker.BreakerConfig(failure_threshold=1, cooldown_seconds=0))
    breaker.record_failure("half-one", "down")

    breaker.record_success("closed-one")
    breaker.record_failure("closed-one", "blip")  # below threshold: still closed

    snap = breaker.snapshot()

    assert snap["open-one"]["state"] == "open"
    assert snap["half-one"]["state"] == "half_open"
    assert snap["closed-one"]["state"] == "closed"
    # And it agrees with the single-breaker path, which is the other reader.
    for name, info in snap.items():
        assert breaker.state(name) == info["state"], f"{name} disagrees between the two paths"


# -------------------------------------------------------------- the blocking


async def test_the_async_snapshot_does_not_block_the_event_loop(monkeypatch):
    """The consequence that is actually felt: a scrape must not freeze the
    loop that is negotiating live calls."""
    def slow_snapshot():
        time.sleep(0.25)
        return {}

    monkeypatch.setattr(breaker, "snapshot", slow_snapshot)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(ticker())
    try:
        await breaker.snapshot_async()
    finally:
        beat.cancel()

    assert ticks > 5, (
        f"the event loop only advanced {ticks} times during a 0.25s snapshot — "
        f"it was blocked"
    )


async def test_the_async_snapshot_returns_the_same_data():
    """Offloading must not change the answer."""
    _open_breakers(3)

    assert await breaker.snapshot_async() == breaker.snapshot()


async def test_metrics_collection_does_not_block_the_event_loop(monkeypatch):
    """End to end through the actual scrape path."""
    from app.core import metrics

    def slow_snapshot():
        time.sleep(0.25)
        return {}

    monkeypatch.setattr(breaker, "snapshot", slow_snapshot)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(ticker())
    try:
        await metrics._build_registry()
    finally:
        beat.cancel()

    assert ticks > 5, f"a Prometheus scrape blocked the loop ({ticks} ticks)"


async def test_health_report_does_not_block_the_event_loop(monkeypatch):
    """The same call sits in health.report(), which the watchdog runs every
    20s and Docker's HEALTHCHECK hits from outside."""
    from app.core import health

    def slow_snapshot():
        time.sleep(0.25)
        return {}

    monkeypatch.setattr(breaker, "snapshot", slow_snapshot)
    # Without this the database ping yields to the loop first and the ticker
    # races ahead before the blocking call is even reached — the test would
    # then pass for a reason that has nothing to do with what it checks.
    async def instant_db(timeout: float = 3.0):
        return True, ""

    monkeypatch.setattr(health, "check_database", instant_db)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(ticker())
    try:
        await health.report()
    finally:
        beat.cancel()

    assert ticks > 5, f"a health check blocked the loop ({ticks} ticks)"
