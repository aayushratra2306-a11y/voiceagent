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
