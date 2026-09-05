"""Phase 4, re-checked — what the new endpoints were giving away.

Both defects here were introduced BY Phase 4 and found by re-reading it,
not by anything failing. They share one shape: monitoring code that was
correct about the numbers and careless about who could read them.

  1. /health is proxied straight to the public internet (deploy/Caddyfile
     has always had a `handle /health` block, because Docker's HEALTHCHECK
     and a load balancer must reach it without credentials). Task 4.7
     replaced its `{"status": "ok"}` body with the full report — so an
     anonymous request to a public URL came back with the hostname of every
     customer API that had tripped a breaker, how many calls were live at
     that instant, and the database's own error text.

  2. A tool URL may legally carry a credential in it
     (https://key:secret@api.customer.com/...). Task 4.6 named each breaker
     after the URL's netloc, which INCLUDES that credential — so it went
     into the breaker store, the logs, and both monitoring endpoints.
"""

from pathlib import Path

import pytest

from app.core import breaker
from app.services import tool_registry
from tests.conftest import auth_headers

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _isolated_breaker_store(tmp_path: Path):
    breaker.use_database(tmp_path / "breakers.db")
    yield


# ---------------------------------------------------------------------------
# 1. the public health endpoint
# ---------------------------------------------------------------------------


async def test_the_public_health_check_still_works_without_credentials(client):
    """It has to. Docker's HEALTHCHECK and any load balancer hit it with no
    token at all, and a health check that 401s is a health check that
    reports every healthy server as broken."""
    resp = await client.get("/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_the_public_health_check_gives_away_nothing_else(client):
    """A tripped breaker names a customer's own API host. That must not be
    readable by anyone who can reach the domain."""
    cfg = breaker.BreakerConfig(failure_threshold=1, cooldown_seconds=30)
    breaker.configure("tool:orders.acme-internal.test", cfg)
    breaker.record_failure("tool:orders.acme-internal.test", "connection refused")

    resp = await client.get("/health")
    body = resp.text

    assert "acme-internal" not in body, "a customer's API hostname was served to an anonymous request"
    assert "circuit_breakers" not in body
    assert "active_calls" not in body
    assert "capacity" not in body
    assert set(resp.json()) == {"status"}, f"public health body grew: {resp.json()}"


async def test_the_detailed_report_requires_a_login(client):
    resp = await client.get("/health/detail")
    assert resp.status_code in (401, 403)


async def test_the_detailed_report_is_there_for_someone_logged_in(client, user_a_token):
    """The information is genuinely useful — the point is who can read it,
    not that it should stop existing."""
    resp = await client.get("/health/detail", headers=auth_headers(user_a_token))

    assert resp.status_code == 200
    body = resp.json()
    assert "circuit_breakers" in body
    assert "database" in body
    assert "capacity" in body


# ---------------------------------------------------------------------------
# the metrics endpoint
# ---------------------------------------------------------------------------


async def test_metrics_are_refused_without_any_credential(client):
    resp = await client.get("/metrics")

    assert resp.status_code == 401
    assert "voiceagent_active_calls" not in resp.text


async def test_metrics_are_served_to_a_logged_in_user(client, user_a_token):
    resp = await client.get("/metrics", headers=auth_headers(user_a_token))

    assert resp.status_code == 200
    assert "voiceagent_active_calls" in resp.text


async def test_a_prometheus_scraper_can_use_a_static_token(client, monkeypatch):
    """Prometheus has no way to refresh a JWT — a static bearer token is
    what its scrape config can actually send."""
    from main import settings as main_settings

    monkeypatch.setattr(main_settings, "metrics_token", "s3cret-scrape-token")

    resp = await client.get(
        "/metrics", headers={"Authorization": "Bearer s3cret-scrape-token"}
    )

    assert resp.status_code == 200
    assert "voiceagent_active_calls" in resp.text


async def test_a_wrong_static_token_is_refused(client, monkeypatch):
    from main import settings as main_settings

    monkeypatch.setattr(main_settings, "metrics_token", "s3cret-scrape-token")

    resp = await client.get("/metrics", headers={"Authorization": "Bearer wrong"})

    assert resp.status_code == 401


async def test_an_unset_static_token_cannot_be_matched_by_an_empty_one(client, monkeypatch):
    """The blank-token trap: with metrics_token unset, a request sending an
    empty bearer must not compare equal to it and sail through."""
    from main import settings as main_settings

    monkeypatch.setattr(main_settings, "metrics_token", "")

    resp = await client.get("/metrics", headers={"Authorization": "Bearer "})

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 2. the credential in the breaker name
# ---------------------------------------------------------------------------


async def test_a_url_embedded_credential_never_becomes_a_breaker_name():
    name = tool_registry._breaker_name("https://apikey:s3cret@api.customer.test/orders/1")

    assert name == "tool:api.customer.test"
    assert "s3cret" not in name
    assert "apikey" not in name


async def test_a_port_still_distinguishes_two_services_on_one_host():
    """Stripping userinfo must not go so far that two genuinely separate
    dependencies start sharing one breaker."""
    assert tool_registry._breaker_name("https://api.test:8443/a") == "tool:api.test:8443"
    assert tool_registry._breaker_name("https://api.test/a") == "tool:api.test"


async def test_a_credentialed_tool_url_never_reaches_the_monitoring_output(monkeypatch):
    """End to end, through the real tool caller and out the endpoints that
    actually serve this: a tool configured with a credential in its URL
    trips a breaker, and neither the breaker store nor anything built from
    it may carry that secret.

    Asserted on breaker.snapshot() rather than the log text on purpose —
    this project logs through loguru, which does not feed pytest's caplog,
    so a caplog assertion here would pass whether the bug existed or not.
    snapshot() is also the thing /health/detail and /metrics are built
    from, which is where a leak would actually end up.
    """
    from app.models.bot_tool import BotTool

    class _Resp:
        def __init__(self):
            self.status_code, self.text, self.headers = 503, "", {}

    class _Client:
        def __init__(self):
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, **kw):
            self.calls += 1
            return _Resp()

    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: _Client())
    tool = BotTool(
        bot_id="bot-1", name="lookup", description="Look up.", kind="http",
        method="GET", url="https://apikey:s3cret@api.customer.test/orders/1",
    )

    for _ in range(4):  # enough 503s to trip it and then be refused
        await tool_registry.call_http_tool(tool, {})

    snapshot = breaker.snapshot()
    assert snapshot, "the breaker never tripped, so this proves nothing"
    for name, info in snapshot.items():
        assert "s3cret" not in name, f"credential leaked into a breaker name: {name}"
        assert "s3cret" not in str(info), f"credential leaked into breaker detail: {info}"
    assert "tool:api.customer.test" in snapshot
