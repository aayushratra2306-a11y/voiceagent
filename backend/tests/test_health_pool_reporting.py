"""Review finding #7 (2026-09-11) — /health lied about an autoscale-to-zero
pool.

`check_worker_pool()` read `call_worker_pool_min == 0` as proof that pooling
was switched off:

    if target == 0:
        return True, "pooling disabled"

But per `next_pool_target()`, pool_min=0 with pool_max>0 is a legitimate and
fully functional configuration: the pool drains to nothing when idle and
grows again the moment a cold-spawn shows demand. An operator choosing it
deliberately — to give an idle server its memory back — got told their
working autoscaler was disabled, on the page they would open to check
exactly that.

Pooling is only genuinely off when pool_max is 0 too; then there is no
capacity to scale into and next_pool_target() can never return anything but
0. That is the condition the check now uses.

The existing test suite only ever exercised pool_min=2, which is why the
misreport survived.
"""

import pytest

from app.core.health import check_worker_pool

# No module-level asyncio mark: check_worker_pool() is synchronous and every
# test here is too.


@pytest.fixture
def pool(monkeypatch):
    """Drive the two settings the check reads, plus the live pool size."""
    from app.api import connect as connect_module

    def configure(pool_min: int, pool_max: int, idle: int = 0):
        monkeypatch.setattr(connect_module.settings, "call_worker_pool_min", pool_min)
        monkeypatch.setattr(connect_module.settings, "call_worker_pool_max", pool_max)
        monkeypatch.setattr(connect_module, "_idle_pool", [object()] * idle)

    return configure


def test_autoscale_to_zero_is_not_reported_as_disabled(pool):
    """The bug. pool_min=0 with room to grow is a working autoscaler."""
    pool(pool_min=0, pool_max=4, idle=0)

    ok, detail = check_worker_pool()

    assert ok
    assert "disabled" not in detail.lower(), (
        f"reported {detail!r} for a pool that is autoscaling 0<->4"
    )


def test_autoscale_to_zero_with_warm_workers_reports_them(pool):
    """And when it has actually scaled up, it should say so rather than
    falling into any special case for the floor being zero."""
    pool(pool_min=0, pool_max=4, idle=3)

    ok, detail = check_worker_pool()

    assert ok
    assert "3" in detail
    assert "disabled" not in detail.lower()


def test_pooling_is_only_disabled_when_there_is_no_room_to_grow(pool):
    """The genuine off switch: no floor AND no ceiling, so
    next_pool_target() can never return anything but zero."""
    pool(pool_min=0, pool_max=0, idle=0)

    ok, detail = check_worker_pool()

    assert ok
    assert "disabled" in detail.lower()


def test_a_conventional_pool_still_reports_as_before(pool):
    """The configuration the existing tests already covered must not change
    behaviour."""
    pool(pool_min=2, pool_max=4, idle=2)

    ok, detail = check_worker_pool()

    assert ok
    assert "2" in detail


def test_an_empty_conventional_pool_still_flags_cold_spawns(pool):
    """A pool with a floor of 2 sitting at 0 is worth saying out loud — that
    is the "silently stuck at zero" case this check exists for."""
    pool(pool_min=2, pool_max=4, idle=0)

    ok, detail = check_worker_pool()

    assert ok, "an empty pool is a latency problem, never a health failure"
    assert "cold" in detail.lower()
