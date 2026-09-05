"""Task 4.6 — the breaker actually wired into the things that fail.

test_circuit_breakers.py covers the breaker's own logic. This covers the two
places it is connected: the customer HTTP tool caller, and the provider
factory that picks speech recognition and speech synthesis for a call.

The distinction that matters most here is between a broken system and a
correct "no". A 404 for an order number the caller misread is a healthy API
answering properly, and a breaker that trips on those would take a working
integration offline because three people in a row read their reference
wrong. Only 5xx, timeouts and connection failures count.
"""

from pathlib import Path

import pytest

from app.core import breaker
from app.models.bot_tool import BotTool
from app.services import tool_registry
from app.services.tool_registry import call_http_tool

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path):
    breaker.use_database(tmp_path / "breakers.db")
    breaker._configs.clear()
    yield
    breaker._configs.clear()


class _Resp:
    def __init__(self, status=200, text='{"ok": true}'):
        self.status_code, self.text = status, text
        self.headers = {}


class _CountingClient:
    """Answers every request the same way and counts how many got through."""

    def __init__(self, response=None, raises=None):
        self.response = response or _Resp()
        self.raises = raises
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kw):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.response


def _tool(name="lookup", url="https://broken.test/orders/1", method="GET") -> BotTool:
    return BotTool(
        bot_id="bot-1", name=name, description="Look something up.",
        kind="http", method=method, url=url,
    )


# ---------------------------------------------------------------------------
# The customer's own API
# ---------------------------------------------------------------------------


async def test_repeated_server_errors_stop_the_calls_going_out(monkeypatch):
    client = _CountingClient(_Resp(status=503, text=""))
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    tool = _tool()

    for _ in range(3):
        await call_http_tool(tool, {})
    assert client.calls == 3

    result = await call_http_tool(tool, {})

    assert client.calls == 3, "the fourth call went out anyway — the breaker did nothing"
    assert result["ok"] is False
    assert result["error"] == "unavailable"


async def test_the_caller_is_told_it_is_unavailable_not_that_they_were_wrong(monkeypatch):
    """A refused call must not come back sounding like the caller gave bad
    details. They did not; the far end is down, and the bot should say so."""
    client = _CountingClient(_Resp(status=500, text=""))
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    tool = _tool()

    for _ in range(3):
        await call_http_tool(tool, {})
    result = await call_http_tool(tool, {})

    message = result["message"].lower()
    assert "unavailable" in message
    assert "confirm them" not in message, "that wording blames the caller for an outage"
    assert "guessing" in message, "the model still must not invent an answer"


async def test_a_refused_call_returns_immediately(monkeypatch):
    """The point of the whole task. A tool with an eight second timeout must
    not cost eight seconds once we already know the host is not answering —
    that time is silence on a live phone call."""
    import time

    client = _CountingClient(raises=tool_registry.httpx.ConnectError("refused"))
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    tool = _tool()

    for _ in range(3):
        await call_http_tool(tool, {})

    started = time.perf_counter()
    await call_http_tool(tool, {})
    elapsed = time.perf_counter() - started

    assert elapsed < 0.1, f"a refused call took {elapsed:.2f}s"


async def test_a_not_found_never_trips_the_breaker(monkeypatch):
    """Three callers misreading their order number is not an outage, and
    taking the order lookup offline over it would be a far worse bug than
    the one this task is fixing."""
    client = _CountingClient(_Resp(status=404, text='{"error": "no such order"}'))
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    tool = _tool()

    for _ in range(6):
        await call_http_tool(tool, {})

    assert client.calls == 6, "a 404 tripped the breaker"
    assert breaker.state("tool:broken.test") == "closed"


async def test_a_rejected_request_never_trips_the_breaker(monkeypatch):
    """Same argument for 400. The API is up and is telling us the request
    was wrong."""
    client = _CountingClient(_Resp(status=400, text=""))
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    tool = _tool()

    for _ in range(6):
        await call_http_tool(tool, {})

    assert client.calls == 6
    assert breaker.state("tool:broken.test") == "closed"


async def test_one_customer_being_down_does_not_block_another(monkeypatch):
    """The breaker is keyed on the host. A shared one would mean one
    customer's outage silenced every other customer's tools."""
    broken = _CountingClient(_Resp(status=503, text=""))
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: broken)
    for _ in range(3):
        await call_http_tool(_tool(url="https://broken.test/x"), {})

    healthy = _CountingClient(_Resp(status=200))
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: healthy)
    result = await call_http_tool(_tool(url="https://healthy.test/x"), {})

    assert result["ok"] is True
    assert healthy.calls == 1


async def test_two_tools_on_one_broken_host_share_the_lesson(monkeypatch):
    """A customer whose API is down usually has several tools pointing at
    it. The second one should not have to rediscover the outage."""
    client = _CountingClient(_Resp(status=503, text=""))
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)

    for _ in range(3):
        await call_http_tool(_tool(name="check_order", url="https://acme.test/orders"), {})

    result = await call_http_tool(_tool(name="check_stock", url="https://acme.test/stock"), {})

    assert result["error"] == "unavailable"
    assert client.calls == 3, "the second tool re-learned an outage the first already found"


async def test_the_host_gets_another_chance_once_it_recovers(monkeypatch):
    """An outage that has ended must not keep the integration switched off.
    After the cooldown one trial goes out, and a good answer restores
    normal service for everybody."""
    import time

    tool_registry.TOOL_BREAKER = breaker.BreakerConfig(
        failure_threshold=3, window_seconds=60.0, cooldown_seconds=0.2
    )
    try:
        broken = _CountingClient(_Resp(status=503, text=""))
        monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: broken)
        tool = _tool(url="https://flaky.test/x")
        for _ in range(3):
            await call_http_tool(tool, {})
        assert (await call_http_tool(tool, {}))["error"] == "unavailable"

        time.sleep(0.25)
        recovered = _CountingClient(_Resp(status=200, text='{"ok": true}'))
        monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: recovered)

        assert (await call_http_tool(tool, {}))["ok"] is True, "the trial call never went out"
        assert (await call_http_tool(tool, {}))["ok"] is True, "the breaker stayed open after a good trial"
        assert recovered.calls == 2
    finally:
        tool_registry.TOOL_BREAKER = breaker.BreakerConfig(
            failure_threshold=3, window_seconds=60.0, cooldown_seconds=20.0
        )


async def test_a_blocked_url_is_refused_before_the_breaker_is_consulted(monkeypatch):
    """Ordering check. The SSRF refusal from Phase 3 must still come first:
    a breaker decision is about availability, and an address this server is
    not allowed to fetch is not an availability question."""
    client = _CountingClient()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    tool = _tool(url="http://169.254.169.254/latest/meta-data/")

    result = await call_http_tool(tool, {})

    assert result["error"] == "blocked_url"
    assert client.calls == 0
    assert breaker.snapshot() == {}, "a refused address should not count against a host"
