"""Review finding I4 (2026-09-10) — the SSRF guard had two holes in it.

app/core/url_safety.py exists because two Phase 3 features take a URL from
outside and then make THIS SERVER fetch it: a webhook subscription's URL
and a bot tool's URL. On a cloud VM `http://169.254.169.254/` is not a
website, it is the metadata service handing out the instance's own
service-account token to anything on the box that asks.

The guard checked the right things and then let them go in two ways, and
both were reachable by an attacker who controls a nameserver — which is
anybody who owns a domain.

  1. It failed OPEN. `_host_is_public` returned None when the resolver
     could not answer, and `rejection_reason` let None through as "fine".
     So the attack was not "point it at 127.0.0.1" (that was caught), it
     was "make the safety check fail". SERVFAIL or simply time out the
     lookup and the URL sails past unchecked.

  2. It checked one address and dialled another. Validation called
     getaddrinfo, then httpx called getaddrinfo AGAIN at connect time, and
     nothing tied the second answer to the first. A nameserver that
     answers the first query with a public address and the second with
     127.0.0.1 defeats the check completely while passing it. That is DNS
     rebinding, and the existing "re-check immediately before the request"
     comment did not help — re-checking earlier is still earlier.

The fix is one idea applied twice: resolve ONCE, refuse if anything about
the answer is wrong or missing, and then connect to that exact address.
"""


import httpx
import pytest

from app.core import url_safety
from app.core.crypto import encrypt_secret
from app.core.url_safety import BlockedAddress, PinnedPublicIPTransport
from app.models.bot_tool import BotTool
from app.models.webhook import WebhookSubscription
from app.services import tool_registry
from app.services import webhooks as webhooks_service

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def dialled(monkeypatch):
    """Records the request that actually reached the socket layer.

    Patched on httpx's own transport, which is what PinnedPublicIPTransport
    delegates to once it is satisfied — so an empty list means the guard
    refused, and a populated one shows exactly what would have gone out on
    the wire.
    """
    sent: list[httpx.Request] = []

    async def fake_dial(self, request):
        sent.append(request)
        return httpx.Response(200, text="{}", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_dial)
    return sent


def _resolves_to(monkeypatch, *answers: list[str]):
    """Make name resolution return a scripted answer per call.

    Several of these tests need the resolver to answer DIFFERENTLY the
    second time, because that is the whole shape of a rebinding attack.
    """
    calls = iter(answers)

    def fake_resolve(host: str) -> list[str]:
        try:
            return next(calls)
        except StopIteration:
            return answers[-1]

    monkeypatch.setattr(url_safety, "_resolve_addresses", fake_resolve)


# =========================================================================
# 1. It failed open when the resolver could not answer
# =========================================================================


async def test_a_name_that_cannot_be_resolved_is_refused(monkeypatch):
    """The old behaviour was documented as deliberate: an unresolvable name
    is not a route into anything, so why fail a delivery over a resolver
    blip?

    Because the attacker chooses the nameserver. Making the lookup fail is
    free for whoever owns the domain, and a check that passes when it
    cannot run is not a check.
    """
    def explode(host):
        raise OSError("SERVFAIL")

    monkeypatch.setattr(url_safety, "_resolve_addresses", explode)

    assert url_safety.rejection_reason("https://attacker-controlled.example/hook") is not None


async def test_a_name_with_no_addresses_at_all_is_refused(monkeypatch):
    """The other empty answer: the query succeeded and returned nothing."""
    _resolves_to(monkeypatch, [])

    assert url_safety.rejection_reason("https://empty.example/hook") is not None


async def test_a_name_that_resolves_publicly_is_still_allowed(monkeypatch):
    """Failing closed must not mean failing always — the ordinary case is a
    customer's real endpoint and it has to keep working."""
    _resolves_to(monkeypatch, ["93.184.216.34"])

    assert url_safety.rejection_reason("https://ordinary.example/hook") is None


# =========================================================================
# 2. It checked one address and dialled another
# =========================================================================


async def test_the_address_that_was_checked_is_the_address_that_is_dialled(
    monkeypatch, dialled
):
    """The connection goes to the IP that passed the check, by number.

    The original hostname still has to travel with it, or the request
    breaks for entirely ordinary reasons: virtual hosts need the Host
    header to route, and TLS needs the name for SNI and for verifying the
    certificate. Pinning the address must not quietly turn certificate
    verification off.
    """
    _resolves_to(monkeypatch, ["93.184.216.34"])
    transport = PinnedPublicIPTransport()

    request = httpx.Request("GET", "https://shop.example.com/orders/1")
    await transport.handle_async_request(request)

    assert len(dialled) == 1
    sent = dialled[0]
    assert sent.url.host == "93.184.216.34", "connected by name, not by the checked address"
    assert sent.headers["Host"] == "shop.example.com"
    assert sent.extensions.get("sni_hostname") == "shop.example.com"
    # The path and scheme are untouched — only where it dials changed.
    assert sent.url.path == "/orders/1"
    assert sent.url.scheme == "https"


async def test_a_name_that_turns_private_between_check_and_connect_never_connects(
    monkeypatch, dialled
):
    """DNS rebinding, which is the attack the old ordering could not see.

    First answer: a perfectly ordinary public address, so validation is
    happy and the URL is stored and later approved for sending. Second
    answer, at connect time: 169.254.169.254. Because the address is now
    resolved once and then dialled by number, there is no second answer to
    be fooled by — and the connect-time resolution is itself validated, so
    a request whose ONLY resolution is the malicious one is refused rather
    than sent.
    """
    _resolves_to(monkeypatch, ["93.184.216.34"], ["169.254.169.254"])

    assert url_safety.rejection_reason("https://rebind.example/hook") is None  # passes the save-time check

    transport = PinnedPublicIPTransport()
    with pytest.raises(BlockedAddress):
        await transport.handle_async_request(httpx.Request("GET", "https://rebind.example/hook"))

    assert dialled == [], "the metadata service was contacted"


async def test_one_private_address_among_several_refuses_the_whole_name(
    monkeypatch, dialled
):
    """A name answering with one public and one loopback address is a
    deliberate attack shape — which of them gets dialled is not ours to
    predict, so neither does."""
    _resolves_to(monkeypatch, ["93.184.216.34", "127.0.0.1"])

    transport = PinnedPublicIPTransport()
    with pytest.raises(BlockedAddress):
        await transport.handle_async_request(httpx.Request("GET", "https://mixed.example/hook"))

    assert dialled == []


async def test_a_resolver_failure_at_connect_time_refuses_rather_than_dialling(
    monkeypatch, dialled
):
    """Fails closed here too, for the same reason it does at validation."""
    def explode(host):
        raise OSError("SERVFAIL")

    monkeypatch.setattr(url_safety, "_resolve_addresses", explode)

    transport = PinnedPublicIPTransport()
    with pytest.raises(BlockedAddress):
        await transport.handle_async_request(httpx.Request("GET", "https://nope.example/hook"))

    assert dialled == []


async def test_a_literal_public_address_is_passed_through_untouched(monkeypatch, dialled):
    """Nothing to resolve and nothing to pin — and no Host header rewrite
    that would make the request look different from what was configured."""
    transport = PinnedPublicIPTransport()

    await transport.handle_async_request(httpx.Request("GET", "https://8.8.8.8/hook"))

    assert len(dialled) == 1
    assert dialled[0].url.host == "8.8.8.8"


async def test_local_development_is_still_allowed_to_reach_localhost(monkeypatch, dialled):
    """`allow_private_outbound_urls` is the documented dev escape hatch —
    pointing a tool at http://localhost:9000 is the normal thing to do
    there. Pinning must not take that away."""
    monkeypatch.setattr(url_safety.settings, "allow_private_outbound_urls", True)
    transport = PinnedPublicIPTransport()

    await transport.handle_async_request(httpx.Request("GET", "http://localhost:9000/hook"))

    assert len(dialled) == 1


# =========================================================================
# 3. The two features that actually make these requests use it
# =========================================================================


class _CapturingClient:
    def __init__(self):
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, *a, **k):
        self.calls += 1
        return httpx.Response(200, text="{}")

    async def post(self, *a, **k):
        self.calls += 1
        return httpx.Response(200, text="{}")


def _capture_client(monkeypatch, module):
    captured: dict = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return _CapturingClient()

    monkeypatch.setattr(module.httpx, "AsyncClient", fake_client)
    return captured


async def test_a_tool_call_goes_out_through_the_pinned_transport(monkeypatch):
    """The guard is worth nothing if the code that makes the request does
    not use it. A literal address is used here so the assertion is about
    the transport and not about DNS."""
    captured = _capture_client(monkeypatch, tool_registry)
    tool = BotTool(
        bot_id="bot-1", name="lookup", description="Look up.", kind="http",
        method="GET", url="https://8.8.8.8/orders",
    )

    await tool_registry.call_http_tool(tool, {})

    assert isinstance(captured.get("transport"), PinnedPublicIPTransport)


async def test_a_webhook_delivery_goes_out_through_the_pinned_transport(monkeypatch):
    captured = _capture_client(monkeypatch, webhooks_service)
    sub = WebhookSubscription(
        user_id="user-x", event="call.ended", url="https://8.8.8.8/hook",
        secret_encrypted=encrypt_secret("s"),
    )

    await webhooks_service.deliver_now(sub, "call.ended", {})

    assert isinstance(captured.get("transport"), PinnedPublicIPTransport)


async def test_the_guard_does_not_cost_a_call_a_sixth_of_a_second(monkeypatch):
    """Routing every outbound request through a transport must not mean
    building a fresh SSL context for every outbound request.

    Constructing one reads and parses the whole CA bundle — measured at
    0.17s here — and a tool call happens in the middle of a live phone
    call, so that would be a sixth of a second of silence per call paid to
    re-read a file that never changes. Caught by the parallel-tools timing
    test when this transport was first added; asserted directly here so
    the reason is written down rather than rediscovered.
    """
    url_safety._ssl_context()  # whoever ran first paid for it; that is the point
    before = url_safety._ssl_context.cache_info()
    url_safety.safe_transport()
    url_safety.safe_transport()
    after = url_safety._ssl_context.cache_info()

    assert after.misses == before.misses, "an SSL context was rebuilt per request"


async def test_a_blocked_address_reaches_the_model_as_a_refusal_not_a_crash(monkeypatch):
    """A tool call never raises — every failure has to become something the
    model can say out loud, or the caller hears dead air. A refusal from
    the transport is no exception, and it must not be reported as a
    generic "unreachable" either: the two mean different things to whoever
    reads the logs afterwards.
    """
    _resolves_to(monkeypatch, ["93.184.216.34"], ["127.0.0.1"])
    tool = BotTool(
        bot_id="bot-1", name="lookup", description="Look up.", kind="http",
        method="GET", url="https://rebind-tool.example/orders",
    )

    result = await tool_registry.call_http_tool(tool, {})

    assert result["ok"] is False
    assert result["error"] == "blocked_url"


async def test_a_blocked_webhook_delivery_is_logged_as_blocked(monkeypatch):
    _resolves_to(monkeypatch, ["93.184.216.34"], ["127.0.0.1"])
    sub = WebhookSubscription(
        user_id="user-x", event="call.ended", url="https://rebind-hook.example/hook",
        secret_encrypted=encrypt_secret("s"),
    )

    result = await webhooks_service.deliver_now(sub, "call.ended", {})

    assert result["ok"] is False
    assert "blocked" in result["error"]
