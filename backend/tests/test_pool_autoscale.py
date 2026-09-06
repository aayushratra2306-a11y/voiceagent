"""Task 4.3 — the worker pool grows and shrinks with demand.

The manual's own two-sided requirement: a burst of calls should visibly
trigger new workers, and they should wind down again afterwards rather than
being paid for all night once the burst passes. `next_pool_target` is the
decision itself, pulled out of the maintenance loop specifically so it can
be tested as a pure function — no real process ever has to be spawned to
verify the rule.
"""

from app.api.connect import _SHRINK_AFTER_QUIET_TICKS, next_pool_target

PLENTY_OF_MEMORY = 4000.0
LOW_MEMORY = 100.0


def test_demand_grows_the_pool_by_one():
    new = next_pool_target(
        current_target=2, exhausted=True, quiet_ticks=0,
        available_memory_mb=PLENTY_OF_MEMORY, pool_min=2, pool_max=4,
        min_free_memory_mb=700,
    )
    assert new == 3


def test_growth_stops_at_the_configured_maximum():
    """Paying for idle capacity all night is the failure the manual warns
    about on the other end — an unbounded pool under a sustained burst
    would eventually do exactly that once the burst ends."""
    new = next_pool_target(
        current_target=4, exhausted=True, quiet_ticks=0,
        available_memory_mb=PLENTY_OF_MEMORY, pool_min=2, pool_max=4,
        min_free_memory_mb=700,
    )
    assert new == 4


def test_it_refuses_to_grow_when_memory_is_actually_tight():
    """An OOM kill during live calls costs far more than one caller paying
    the slow cold-spawn path once — growing into that trade is worse than
    not growing at all."""
    new = next_pool_target(
        current_target=2, exhausted=True, quiet_ticks=0,
        available_memory_mb=LOW_MEMORY, pool_min=2, pool_max=4,
        min_free_memory_mb=700, worker_memory_mb=300,
    )
    assert new == 2


def test_a_single_quiet_tick_does_not_shrink_anything():
    """Shrinking eagerly would just re-grow on the very next call and
    repeat forever — the whole reason there is a quiet-ticks threshold at
    all rather than shrinking the moment nothing happened."""
    new = next_pool_target(
        current_target=4, exhausted=False, quiet_ticks=1,
        available_memory_mb=PLENTY_OF_MEMORY, pool_min=2, pool_max=4,
        min_free_memory_mb=700,
    )
    assert new == 4


def test_enough_quiet_ticks_shrinks_by_one():
    new = next_pool_target(
        current_target=4, exhausted=False, quiet_ticks=_SHRINK_AFTER_QUIET_TICKS,
        available_memory_mb=PLENTY_OF_MEMORY, pool_min=2, pool_max=4,
        min_free_memory_mb=700,
    )
    assert new == 3


def test_shrinking_stops_at_the_configured_minimum():
    """A quiet server must never scale all the way to zero — that would pay
    the full cold-start cost (measured at 13.7s) on every single call."""
    new = next_pool_target(
        current_target=2, exhausted=False, quiet_ticks=_SHRINK_AFTER_QUIET_TICKS,
        available_memory_mb=PLENTY_OF_MEMORY, pool_min=2, pool_max=4,
        min_free_memory_mb=700,
    )
    assert new == 2


def test_demand_wins_over_a_shrink_that_would_otherwise_fire():
    """Ordering matters in the function's own priority: exhaustion is
    checked first. A quiet streak long enough to shrink, immediately
    followed by real demand, must grow rather than shrink."""
    new = next_pool_target(
        current_target=3, exhausted=True, quiet_ticks=_SHRINK_AFTER_QUIET_TICKS,
        available_memory_mb=PLENTY_OF_MEMORY, pool_min=2, pool_max=4,
        min_free_memory_mb=700,
    )
    assert new == 4


def test_setting_min_equal_to_max_pins_the_pool_exactly_where_it_was():
    """The pre-4.3 behaviour (a fixed pool size) must still be reachable —
    an operator who sets min == max should get back exactly that."""
    grown = next_pool_target(
        current_target=2, exhausted=True, quiet_ticks=0,
        available_memory_mb=PLENTY_OF_MEMORY, pool_min=2, pool_max=2,
        min_free_memory_mb=700,
    )
    shrunk = next_pool_target(
        current_target=2, exhausted=False, quiet_ticks=_SHRINK_AFTER_QUIET_TICKS,
        available_memory_mb=PLENTY_OF_MEMORY, pool_min=2, pool_max=2,
        min_free_memory_mb=700,
    )
    assert grown == 2
    assert shrunk == 2


def test_two_top_ups_at_once_cannot_overshoot_the_target(monkeypatch):
    """Found on a second read of Phase 4.

    _top_up_pool runs from two places: the maintenance loop, and connect()
    on EVERY call — both through run_in_executor, so both on real threads.
    Without a lock they interleave on the check: each reads
    len(_idle_pool) == 0 against a target of 2, and each spawns two,
    leaving four. Every extra worker holds ~300MB on a 4GB VM, so this
    walks straight past the memory guard the grow path checks — which is
    worse than having no guard, because the configured number stops
    meaning anything.

    Real threads and a real (slow) spawn, because the race only exists in
    the window where one thread is spawning and the other checks the
    length.
    """
    import threading
    import time as time_module

    from app.api import connect as connect_module

    monkeypatch.setattr(connect_module, "_idle_pool", [])
    monkeypatch.setattr(connect_module, "_pool_target", 2)

    def _slow_spawn():
        time_module.sleep(0.05)  # stands in for spawning an interpreter
        return object()

    monkeypatch.setattr(connect_module, "_spawn_pooled_worker", _slow_spawn)

    threads = [threading.Thread(target=connect_module._top_up_pool) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(connect_module._idle_pool) == 2, (
        f"four concurrent top-ups produced {len(connect_module._idle_pool)} workers for a "
        f"target of 2 — roughly {300 * (len(connect_module._idle_pool) - 2)}MB of overshoot "
        f"the memory guard never got to see"
    )


def test_a_shrink_that_lost_its_race_does_not_go_below_target(monkeypatch):
    """The other side of the same lock. If a call claims the last spare
    worker between the loop deciding to shrink and the shrink running,
    retiring one anyway would drop the pool under target and make the next
    caller pay a cold start for nothing."""
    from app.api import connect as connect_module

    monkeypatch.setattr(connect_module, "_idle_pool", [object(), object()])
    monkeypatch.setattr(connect_module, "_pool_target", 2)

    connect_module._shrink_pool_by_one()

    assert len(connect_module._idle_pool) == 2, "shrank a pool that was already at target"


def test_the_cold_spawn_path_actually_raises_the_demand_signal():
    """Wiring check: exhaustion must be evidence-based, not guessed at by
    the maintenance loop. The one place that genuinely knows supply fell
    short is the cold-spawn branch in connect() itself."""
    import inspect

    from app.api import connect as connect_module

    source = inspect.getsource(connect_module.connect)
    assert "_pool_exhausted_since_last_check = True" in source


def test_the_maintenance_loop_actually_calls_the_decision_function():
    import inspect

    from app.api import connect as connect_module

    source = inspect.getsource(connect_module.maintain_worker_pool_loop)
    assert "next_pool_target(" in source
