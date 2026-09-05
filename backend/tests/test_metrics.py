"""Task 4.9 — the /metrics scrape endpoint.

Not testing Prometheus itself, just this project's own two decisions: what
gets exported (system-level numbers this process already knows — never a
transcript, never a caller's data), and that turning it off via settings
actually turns it off.
"""

from pathlib import Path

import pytest

from app.core import breaker, metrics

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _isolated_breaker_store(tmp_path: Path):
    breaker.use_database(tmp_path / "breakers.db")
    yield


async def test_the_export_is_valid_prometheus_exposition_format():
    body, content_type = await metrics.render()

    assert "text/plain" in content_type
    text = body.decode()
    assert "voiceagent_active_calls" in text
    assert "voiceagent_call_capacity_limit" in text
    assert "voiceagent_warm_pool_size" in text
    assert "voiceagent_mongo_expected_max_connections" in text


async def test_a_tripped_breaker_appears_by_name():
    cfg = breaker.BreakerConfig(failure_threshold=1, cooldown_seconds=30)
    breaker.configure("tool:acme.test", cfg)
    breaker.record_failure("tool:acme.test", "connection refused")

    body, _ = await metrics.render()
    text = body.decode()

    assert 'voiceagent_circuit_breaker_state{name="tool:acme.test"} 2.0' in text, text


async def test_a_reset_breaker_does_not_linger_in_the_next_scrape():
    """Built fresh each scrape rather than off prometheus_client's shared
    default registry — otherwise a breaker that tripped once, three deploys
    ago, would report its last value forever even after being cleared."""
    cfg = breaker.BreakerConfig(failure_threshold=1, cooldown_seconds=30)
    breaker.configure("tool:acme.test", cfg)
    breaker.record_failure("tool:acme.test", "connection refused")
    breaker.reset("tool:acme.test")

    body, _ = await metrics.render()
    assert "tool:acme.test" not in body.decode()


async def test_no_caller_data_is_ever_exported():
    """A metrics endpoint is typically unauthenticated (a Prometheus
    scraper is not a logged-in user) — nothing here may be a transcript,
    a phone number, or any other caller-identifying value."""
    body, _ = await metrics.render()
    text = body.decode().lower()

    for forbidden in ("transcript", "caller", "phone", "email", "@"):
        assert forbidden not in text, f"found '{forbidden}' in the metrics export"


async def test_one_broken_reading_does_not_take_the_whole_scrape_with_it():
    """A monitoring endpoint that returns nothing when a dependency is
    broken is useless at precisely the moment it is needed. If Redis is
    unreachable the active-call count is unknowable — but the circuit
    breakers, which are local and very likely to say WHY, still are not."""
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

    breaker.configure("tool:acme.test", breaker.BreakerConfig(failure_threshold=1))
    breaker.record_failure("tool:acme.test", "connection refused")

    call_capacity.use_backend(_ExplodingCapacity())
    try:
        body, _ = await metrics.render()
    finally:
        call_capacity.use_backend(call_capacity._InProcessCapacity())

    text = body.decode()
    assert 'voiceagent_circuit_breaker_state{name="tool:acme.test"}' in text, (
        "one unreadable value threw away every other reading in the scrape"
    )
    assert "voiceagent_metrics_collection_errors 1.0" in text, (
        "a partial scrape reported itself as complete, which looks identical "
        "to a healthy quiet system"
    )


async def test_a_complete_scrape_reports_no_collection_errors():
    body, _ = await metrics.render()
    assert "voiceagent_metrics_collection_errors 0.0" in body.decode()


async def test_the_endpoint_can_be_switched_off(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from main import app
    from main import settings as main_settings

    monkeypatch.setattr(main_settings, "metrics_enabled", False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")

    assert resp.status_code == 404


async def test_the_endpoint_serves_metrics_to_an_authorised_scraper(monkeypatch):
    """Authorised, not anonymous — see test_phase4_disclosure.py for why
    that changed: this endpoint names the customer hostnames that have
    tripped a breaker and reports how many calls are live right now."""
    from httpx import ASGITransport, AsyncClient

    from main import app
    from main import settings as main_settings

    monkeypatch.setattr(main_settings, "metrics_enabled", True)
    monkeypatch.setattr(main_settings, "metrics_token", "a-scrape-token")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/metrics", headers={"Authorization": "Bearer a-scrape-token"}
        )

    assert resp.status_code == 200
    assert "voiceagent_active_calls" in resp.text
