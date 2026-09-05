"""Task 4.9 — the Prometheus scrape endpoint.

Building the actual dashboard (Grafana panels, alert routing) is not
something this can do unattended: it is the user's own Grafana instance,
their own account, their own alert destinations (Slack, email, PagerDuty) —
an infrastructure and access decision, exactly like the Sentry DSN and
Langfuse credentials already sitting blank in config.py waiting for the
same kind of decision. What belongs in code, and what this file is, is the
exporter: a `/metrics` endpoint any Prometheus instance can be pointed at
the moment one exists, reporting real numbers rather than nothing.

Two things distinguish what is exported here from a metric that would need
real work to get right, given task 2.4's architecture:

  - System-level numbers this API process already knows first-hand — the
    circuit breakers (task 4.6, whose state lives in SQLite and is already
    cross-process for free), the concurrency cap (task 4.5), the warm pool
    (task 4.3), expected Mongo connections (task 4.4) — cost nothing extra
    to export: the state exists, this just formats it.
  - Per-CALL numbers — latency percentiles, cost per call, turns per
    conversation — are deliberately NOT duplicated here. Task 2.7 already
    exports exactly that, per call, to Langfuse, from inside each call's own
    process (see tracing.py) — which is the right place for it, since a
    call's own process is what actually has those numbers. Building a
    second, separate pipeline to get the same figures into Prometheus
    (which would need prometheus_client's multiprocess mode: a shared
    directory, per-process file cleanup on exit, a special collector) would
    be real, nontrivial work in service of information a working dashboard
    already has a home for. The manual's own tool list for this task names
    both Prometheus and Langfuse for a reason — they cover different halves,
    not the same one twice.

The 95th/99th percentile point the manual makes ("the average hides the
problem completely") is exactly why call latency staying on Langfuse's
per-call traces, rather than being collapsed into an average gauge here, is
the right call and not a shortcut.
"""

from __future__ import annotations

from loguru import logger
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest

from app.core import breaker
from app.core.call_capacity import active_call_count
from app.core.config import settings
from app.db.mongo import expected_max_connections

# A fresh registry per call to /export() rather than the default global one.
# The default registry (prometheus_client's module-level REGISTRY) persists
# metric objects across every request Gauge.set() ever touched, which is
# fine for counters that only go up but wrong for something like a circuit
# breaker label that can disappear (a breaker reset, or a tool deleted) —
# the old label would otherwise report its last value forever. Building the
# registry fresh each scrape means what's reported is exactly what's true
# right now, nothing stale left over from a customer's tool three deploys
# ago.
_BREAKER_STATE_VALUE = {"closed": 0, "half_open": 1, "open": 2}


async def _build_registry() -> CollectorRegistry:
    registry = CollectorRegistry()

    active_calls = Gauge(
        "voiceagent_active_calls", "Calls currently in progress on this node",
        registry=registry,
    )
    call_capacity_limit = Gauge(
        "voiceagent_call_capacity_limit",
        "Task 4.5's configured ceiling on simultaneous calls (0 = uncapped)",
        registry=registry,
    )
    warm_pool_size = Gauge(
        "voiceagent_warm_pool_size", "Task 2.4/4.3 warm call-worker pool: workers currently idle",
        registry=registry,
    )
    warm_pool_target = Gauge(
        "voiceagent_warm_pool_target", "Task 4.3's current autoscale target for the warm pool",
        registry=registry,
    )
    mongo_expected_max_connections = Gauge(
        "voiceagent_mongo_expected_max_connections",
        "Task 4.4 — worst-case simultaneous Mongo connections this deployment could open",
        registry=registry,
    )
    breaker_state = Gauge(
        "voiceagent_circuit_breaker_state",
        "0=closed 1=half_open 2=open, per circuit breaker (task 4.6)",
        ["name"], registry=registry,
    )
    breaker_trips = Gauge(
        "voiceagent_circuit_breaker_trips_total",
        "How many times this breaker has opened since the process started",
        ["name"], registry=registry,
    )

    collection_errors = Gauge(
        "voiceagent_metrics_collection_errors",
        "How many of this scrape's own readings failed to collect (0 = a complete scrape)",
        registry=registry,
    )

    from app.api import connect as connect_module

    # Each reading collected independently, because a monitoring endpoint
    # that returns nothing when one dependency is broken is useless exactly
    # when it is needed most. If Redis is unreachable, the active-call count
    # is unknowable — but the circuit breakers, which are local and are very
    # likely to say WHY, still are not. One failed reading must not take the
    # rest of the scrape with it.
    failures = 0

    def _collect(gauge, produce, *labels):
        nonlocal failures
        try:
            target = gauge.labels(*labels) if labels else gauge
            target.set(produce())
        except Exception as e:
            failures += 1
            logger.warning(f"[METRICS] could not collect {gauge._name}: {type(e).__name__}: {e}")

    try:
        active_calls.set(await active_call_count())
    except Exception as e:
        failures += 1
        logger.warning(f"[METRICS] could not collect active call count: {type(e).__name__}: {e}")

    _collect(call_capacity_limit, lambda: settings.max_concurrent_calls)
    _collect(warm_pool_size, lambda: len(connect_module._idle_pool))
    _collect(warm_pool_target, lambda: connect_module._pool_target)
    _collect(mongo_expected_max_connections, expected_max_connections)

    try:
        for name, info in breaker.snapshot().items():
            breaker_state.labels(name=name).set(_BREAKER_STATE_VALUE.get(info["state"], -1))
            breaker_trips.labels(name=name).set(info["trips"])
    except Exception as e:
        failures += 1
        logger.warning(f"[METRICS] could not collect breaker states: {type(e).__name__}: {e}")

    # Reported rather than hidden: a scrape that silently dropped half its
    # readings looks identical to a healthy quiet system, and an alert on
    # this is how you find out the monitoring itself is broken.
    collection_errors.set(failures)
    return registry


async def render() -> tuple[bytes, str]:
    """The full exposition-format payload and its content type, ready to
    hand straight to a Response. generate_latest() itself is sync (it just
    formats numbers already collected above), so only the collection step
    needs to be async — for the one figure here whose backend can genuinely
    be remote (active_call_count(), which talks to Redis once a second API
    replica exists — see call_capacity.py)."""
    registry = await _build_registry()
    return generate_latest(registry), CONTENT_TYPE_LATEST
