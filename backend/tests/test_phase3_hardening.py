"""Phase 3, re-checked — the defects a second pass over 3.1-3.10 turned up.

Every test here failed before the fix it covers. They are grouped by the
thing that was actually wrong rather than by task, because several of the
faults sat in the seam BETWEEN two tasks and belong to neither on its own:
the worst of them (a long-running tool skipping the approval gate) needs
3.3 and 3.10 to be configured on the same tool, which is exactly the
combination a real "large refund through a slow provider" would use.

The grouping, worst first:

  1. the approval gate could be walked straight past
  2. approving twice ran the action twice
  3. a large-but-valid response was reported to callers as "not found"
  4. this server would fetch any address a customer named, including its
     own cloud metadata service
  5. a slow customer endpoint received the same event over and over
  6. one customer's payment webhook could resolve to another's payment
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.core.crypto import encrypt_secret
from app.models.approval import PendingApproval
from app.models.bot_tool import BotTool
from app.models.payment import PaymentSession
from app.models.webhook import WebhookOutboxItem, WebhookSubscription
from app.pipeline import call_context
from app.services import tool_registry
from app.services import webhooks as webhooks_service
from app.services.tool_registry import call_http_tool, to_function_schema
from tests.conftest import auth_headers

# The token fixtures hand back a JWT whose `sub` is the account's email,
# while the approvals API scopes by the real Mongo id — this resolves the
# one to the other exactly as get_current_user does. Already written for
# task 3.10's own tests; imported rather than duplicated.
from tests.test_approvals import _user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _Resp:
    def __init__(self, status=200, text='{"ok": true}'):
        self.status_code, self.text = status, text


class _Client:
    """Records what actually went out, and how many times."""

    def __init__(self, response=None):
        self.response = response or _Resp()
        self.calls = 0
        self.seen: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kw):
        self.calls += 1
        self.seen = {"method": method, "url": url, **kw}
        return self.response

    async def post(self, url, **kw):
        self.calls += 1
        self.seen = {"url": url, **kw}
        return _Resp(self.response.status_code)


class _HandlerParams:
    """Stands in for pipecat's FunctionCallParams. The gate lives in the
    generated handler, not in call_http_tool — see test_approvals.py."""

    def __init__(self, arguments):
        self.arguments = arguments
        self.result = None

    async def result_callback(self, result):
        self.result = result


async def _call_gated(tool: BotTool, args: dict, jobs=None) -> dict:
    params = _HandlerParams(args)
    await to_function_schema(tool, jobs=jobs).handler(params)
    return params.result


@pytest.fixture(autouse=True)
def _clean_context():
    call_context.clear()
    yield
    call_context.clear()


# =========================================================================
# 1. The approval gate could be walked straight past
# =========================================================================


class _RecordingJobs:
    """Task 3.3's background runner, reduced to "did anything get handed
    to it". Its `start` is what would actually execute the tool for real."""

    def __init__(self):
        self.started: list[str] = []

    def start(self, name, coro, args):
        self.started.append(name)
        # The real runner awaits this. Close it so the event loop does not
        # warn about a coroutine that was never awaited — the point of the
        # test is that we should never have been handed it at all.
        coro.close()


def _slow_and_gated_tool() -> BotTool:
    """The configuration this bug lived in: a big refund against a payment
    provider slow enough to be worth backgrounding. Both settings are
    individually sensible and the manual suggests both."""
    return BotTool(
        bot_id="bot-1", name="issue_large_refund", description="Refund a customer.",
        kind="http", method="POST", url="https://api.test/refunds",
        long_running=True,
        approval={"enabled": True, "amount_parameter": "amount", "threshold": 100.0},
    )


async def test_a_long_running_tool_still_has_to_pass_the_approval_gate(monkeypatch):
    """THE one that mattered. The gate used to sit after the long-running
    branch, so a tool that was both simply dispatched itself to the
    background and ran for real, with no approval ever created and nothing
    logged to say so."""
    client = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    jobs = _RecordingJobs()

    result = await _call_gated(_slow_and_gated_tool(), {"amount": 5000}, jobs=jobs)

    assert result["pending_approval"] is True
    assert jobs.started == [], "the action was handed to the background runner un-approved"
    assert client.calls == 0, "the customer's API was called without approval"

    queued = await PendingApproval.find(PendingApproval.tool_name == "issue_large_refund").to_list()
    assert len(queued) == 1
    assert queued[0].status == "pending"
    for a in queued:
        await a.delete()


async def test_a_long_running_tool_under_the_threshold_still_goes_to_the_background(monkeypatch):
    """The fix must not cost task 3.3 its behaviour: below the threshold
    the tool is backgrounded exactly as before, so the caller still hears
    an acknowledgement instead of silence."""
    client = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    jobs = _RecordingJobs()

    result = await _call_gated(_slow_and_gated_tool(), {"amount": 20}, jobs=jobs)

    assert result["started"] is True
    assert jobs.started == ["issue_large_refund"]


async def test_a_long_running_tool_with_no_gate_is_unaffected(monkeypatch):
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: _Client())
    jobs = _RecordingJobs()
    tool = BotTool(
        bot_id="bot-1", name="slow_lookup", description="Slow.", kind="http",
        method="GET", url="https://api.test/x", long_running=True,
    )

    result = await _call_gated(tool, {}, jobs=jobs)

    assert result["started"] is True
    assert jobs.started == ["slow_lookup"]


# =========================================================================
# 2. Approving twice ran the action twice
# =========================================================================


async def _pending_approval_for(user_id: str, tool_id: str) -> PendingApproval:
    approval = PendingApproval(
        tool_id=tool_id, bot_id="bot-1", user_id=user_id, tool_name="issue_refund",
        arguments={"amount": 500}, amount=500.0, threshold=100.0,
    )
    await approval.insert()
    return approval


async def test_approving_the_same_thing_twice_runs_the_action_once(
    client, user_a_token, monkeypatch
):
    """A double-click, a second tab, an impatient retry. Reading "is it
    still pending" and then acting on it is two operations, and two
    requests could both pass the read before either wrote — issuing the
    refund twice, which is the exact outcome this whole task exists to
    prevent."""
    user_id = str(await _user_id(client, user_a_token))

    tool = BotTool(
        bot_id="bot-1", name="issue_refund", description="Refund.", kind="http",
        method="POST", url="https://api.test/refunds",
    )
    await tool.insert()
    approval = await _pending_approval_for(user_id, str(tool.id))

    http = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: http)

    first, second = await asyncio.gather(
        client.post(f"/approvals/{approval.id}/approve", headers=auth_headers(user_a_token)),
        client.post(f"/approvals/{approval.id}/approve", headers=auth_headers(user_a_token)),
        return_exceptions=True,
    )
    codes = sorted(r.status_code for r in (first, second))

    assert codes == [200, 409], f"both requests were accepted: {codes}"
    assert http.calls == 1, f"the customer's API was called {http.calls} times, not once"

    await approval.delete()
    await tool.delete()


async def test_only_one_decision_can_ever_claim_an_approval(client, user_a_token):
    """The deterministic half of the test above.

    Whether two real HTTP requests actually interleave at the unsafe point
    depends on how the event loop happens to schedule them, so the
    end-to-end test is a good check but not a guaranteed reproducer. This
    one goes straight at the mechanism: two claims on the same record,
    exactly one of which may succeed. That is a single atomic update
    either way, so it settles the same question without depending on
    timing.
    """
    from app.api.approvals import _claim_for_decision

    user_id = str(await _user_id(client, user_a_token))
    approval = await _pending_approval_for(user_id, "507f1f77bcf86cd799439011")

    a, b = await asyncio.gather(
        _claim_for_decision(approval, "approving", "someone@example.com"),
        _claim_for_decision(approval, "approving", "someone@example.com"),
    )

    assert sorted([a, b]) == [False, True]

    await approval.delete()


async def test_approve_and_deny_racing_cannot_both_win(client, user_a_token, monkeypatch):
    user_id = str(await _user_id(client, user_a_token))
    tool = BotTool(
        bot_id="bot-1", name="issue_refund", description="Refund.", kind="http",
        method="POST", url="https://api.test/refunds",
    )
    await tool.insert()
    approval = await _pending_approval_for(user_id, str(tool.id))
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: _Client())

    a, d = await asyncio.gather(
        client.post(f"/approvals/{approval.id}/approve", headers=auth_headers(user_a_token)),
        client.post(f"/approvals/{approval.id}/deny", headers=auth_headers(user_a_token)),
    )

    assert sorted([a.status_code, d.status_code]) == [200, 409]
    settled = await PendingApproval.get(approval.id)
    assert settled.status in {"approved", "denied"}

    await approval.delete()
    await tool.delete()


# =========================================================================
# 3. A large-but-valid response was reported to callers as "not found"
# =========================================================================


def _lookup_tool() -> BotTool:
    return BotTool(
        bot_id="bot-1", name="order_status", description="Look up an order.",
        kind="http", method="GET", url="https://api.test/orders/1",
        field_map={"status": "order.status", "eta": "order.eta"},
    )


async def test_a_large_response_still_resolves_its_mapped_fields(monkeypatch):
    """The response body used to be cut to 4000 characters BEFORE being
    parsed, which turned valid JSON into invalid JSON, which fell through
    to "treat it as plain text", at which point every mapped path resolved
    to None and the tool reported the order as not found. A fat order
    record is a completely ordinary thing for a real API to return."""
    big = {
        "order": {"status": "shipped", "eta": "Tuesday"},
        "history": [{"note": "x" * 200} for _ in range(60)],  # well past 4000 chars
    }
    import json as _json

    assert len(_json.dumps(big)) > tool_registry.MAX_RESPONSE_CHARS
    monkeypatch.setattr(
        tool_registry.httpx, "AsyncClient",
        lambda **k: _Client(_Resp(text=_json.dumps(big))),
    )

    result = await call_http_tool(_lookup_tool(), {})

    assert result["ok"] is True
    assert result.get("found") is not False, "a returned order was reported as not found"
    assert result["fields"] == {"status": "shipped", "eta": "Tuesday"}


async def test_a_large_response_is_still_bounded_before_it_reaches_the_model(monkeypatch):
    """The size limit is still real — it just applies to what goes to the
    model, not to what gets parsed."""
    import json as _json

    big = {"order": {"status": "shipped", "eta": "Tuesday"}, "blob": "y" * 20000}
    monkeypatch.setattr(
        tool_registry.httpx, "AsyncClient",
        lambda **k: _Client(_Resp(text=_json.dumps(big))),
    )

    result = await call_http_tool(_lookup_tool(), {})

    assert result["data"]["truncated"] is True
    assert len(result["data"]["preview"]) <= tool_registry.MAX_RESPONSE_CHARS
    assert result["fields"]["status"] == "shipped"


async def test_a_small_response_is_passed_through_untouched(monkeypatch):
    monkeypatch.setattr(
        tool_registry.httpx, "AsyncClient",
        lambda **k: _Client(_Resp(text='{"order": {"status": "packing", "eta": "Fri"}}')),
    )

    result = await call_http_tool(_lookup_tool(), {})

    assert result["data"] == {"order": {"status": "packing", "eta": "Fri"}}


async def test_an_empty_mapped_response_is_still_reported_as_not_found(monkeypatch):
    """Task 3.6's real not-found case must survive the change."""
    monkeypatch.setattr(
        tool_registry.httpx, "AsyncClient", lambda **k: _Client(_Resp(text='{"order": null}')),
    )

    result = await call_http_tool(_lookup_tool(), {})

    assert result["found"] is False


# =========================================================================
# 4. Substituted arguments could reshape the request
# =========================================================================


async def test_an_argument_cannot_add_a_query_parameter_of_its_own(monkeypatch):
    """These arguments come from a model transcribing an anonymous caller,
    which is as untrusted as input gets. A URL is one flat string in which
    `?` and `&` are grammar, so raw substitution let a value change the
    shape of the request rather than only its content."""
    client = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    tool = BotTool(
        bot_id="bot-1", name="order_status", description="Look up.", kind="http",
        method="GET", url="https://api.test/orders/{order_id}",
    )

    await call_http_tool(tool, {"order_id": "1?role=admin&all=true"})

    assert "?role=admin" not in client.seen["url"]
    assert client.seen["url"] == "https://api.test/orders/1%3Frole%3Dadmin%26all%3Dtrue"


async def test_an_argument_cannot_walk_up_out_of_its_path(monkeypatch):
    client = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    tool = BotTool(
        bot_id="bot-1", name="order_status", description="Look up.", kind="http",
        method="GET", url="https://api.test/orders/{order_id}",
    )

    result = await call_http_tool(tool, {"order_id": "../../admin/users"})

    assert result["ok"] is False
    assert client.calls == 0, "a request was sent to a path the tool was not configured for"


async def test_a_value_spanning_path_segments_still_works(monkeypatch):
    """The encoding deliberately leaves `/` alone: a reference that spans
    segments is a real configuration and must not break."""
    client = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    tool = BotTool(
        bot_id="bot-1", name="order_status", description="Look up.", kind="http",
        method="GET", url="https://api.test/orders/{ref}",
    )

    await call_http_tool(tool, {"ref": "2026/17"})

    assert client.seen["url"] == "https://api.test/orders/2026/17"


async def test_a_header_value_cannot_smuggle_a_newline(monkeypatch):
    client = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    tool = BotTool(
        bot_id="bot-1", name="lookup", description="Look up.", kind="http",
        method="GET", url="https://api.test/x", headers={"X-Ref": "{ref}"},
    )

    await call_http_tool(tool, {"ref": "abc\r\nX-Admin: true"})

    assert "\n" not in client.seen["headers"]["X-Ref"]
    assert "\r" not in client.seen["headers"]["X-Ref"]


# =========================================================================
# 5. This server would fetch any address a customer named
# =========================================================================


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/computeMetadata/v1/",  # the cloud metadata service
        "http://127.0.0.1:8000/admin",
        "http://localhost:27017/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://[::1]:8000/",
        "http://user@169.254.169.254/",  # userinfo does not change the host
    ],
)
def test_an_internal_address_is_refused(url):
    from app.core.url_safety import rejection_reason

    assert rejection_reason(url) is not None, f"{url} was allowed"


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "", "https://"])
def test_a_url_that_is_not_a_fetchable_http_address_is_refused(url):
    from app.core.url_safety import rejection_reason

    assert rejection_reason(url) is not None


def test_a_public_address_is_allowed():
    from app.core.url_safety import rejection_reason

    assert rejection_reason("https://8.8.8.8/hook") is None


def test_a_name_that_does_not_resolve_is_allowed_through():
    """Fails open on DNS: an unresolvable name is not a route into
    anything, and treating a resolver blip as an attack would turn it into
    a delivery outage for nothing."""
    from app.core.url_safety import rejection_reason

    assert rejection_reason("https://nx-does-not-exist.invalid/hook") is None


async def test_a_webhook_subscription_cannot_be_pointed_at_the_metadata_service(
    client, user_a_token
):
    resp = await client.post(
        "/webhooks/",
        json={"event": "call.ended", "url": "http://169.254.169.254/computeMetadata/v1/"},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 422


async def test_delivery_refuses_a_blocked_url_even_if_it_was_somehow_stored(monkeypatch):
    """The check that counts is the one immediately before the request: a
    name that resolved publicly when it was saved can be re-pointed at
    127.0.0.1 afterwards."""
    sent = _Client()
    monkeypatch.setattr(webhooks_service.httpx, "AsyncClient", lambda **k: sent)
    sub = WebhookSubscription(
        user_id="user-x", event="call.ended", url="http://127.0.0.1:9/hook",
        secret_encrypted=encrypt_secret("s"),
    )

    result = await webhooks_service.deliver_now(sub, "call.ended", {})

    assert result["ok"] is False
    assert "blocked" in result["error"]
    assert sent.calls == 0


async def test_a_tool_cannot_be_pointed_at_an_internal_address(monkeypatch):
    client = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    tool = BotTool(
        bot_id="bot-1", name="lookup", description="Look up.", kind="http",
        method="GET", url="http://169.254.169.254/computeMetadata/v1/",
    )

    result = await call_http_tool(tool, {})

    assert result["ok"] is False
    assert client.calls == 0


# =========================================================================
# 6. A slow customer endpoint received the same event over and over
# =========================================================================


async def _queued(sub_id: str) -> WebhookOutboxItem:
    item = WebhookOutboxItem(
        subscription_id=sub_id, event="call.ended", payload={"n": 1},
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await item.insert()
    return item


async def test_one_item_is_only_ever_claimed_once(monkeypatch):
    """The loop polls every 5 seconds and one attempt may take 8, so an
    item used to still look "due" while its own request was in flight —
    and got sent again, and again, for as long as the first attempt ran.
    A customer who books or charges on receipt saw duplicates."""
    sub = WebhookSubscription(
        user_id="user-dup", event="call.ended", url="https://example.com/hook",
        secret_encrypted=encrypt_secret("s"),
    )
    await sub.insert()
    item = await _queued(str(sub.id))

    # Two passes racing for the same row, which is what two overlapping
    # polls actually are.
    a, b = await asyncio.gather(
        webhooks_service._claim(await WebhookOutboxItem.get(item.id)),
        webhooks_service._claim(await WebhookOutboxItem.get(item.id)),
    )

    assert sorted([a, b]) == [False, True], "both passes claimed the same delivery"

    await item.delete()
    await sub.delete()


async def test_a_claimed_item_is_not_due_again_until_its_lease_expires(monkeypatch):
    sub = WebhookSubscription(
        user_id="user-lease", event="call.ended", url="https://example.com/hook",
        secret_encrypted=encrypt_secret("s"),
    )
    await sub.insert()
    item = await _queued(str(sub.id))

    assert await webhooks_service._claim(await WebhookOutboxItem.get(item.id)) is True

    refreshed = await WebhookOutboxItem.get(item.id)
    # PyMongo hands back naive datetimes on this client — see project notes.
    next_at = refreshed.next_attempt_at.replace(tzinfo=UTC)
    assert next_at > datetime.now(UTC) + timedelta(seconds=webhooks_service.DELIVERY_TIMEOUT_SECONDS)
    assert refreshed.attempt == 1

    await item.delete()
    await sub.delete()


async def test_a_delivery_that_finds_no_subscription_is_written_to_the_log(monkeypatch):
    """Events used to vanish with nothing recorded, leaving a customer
    asking "why did these stop" with nothing to read."""
    from app.models.webhook import WebhookDelivery

    item = await _queued("507f1f77bcf86cd799439011")  # a well-formed id that resolves to nothing
    await webhooks_service._process_one(await WebhookOutboxItem.get(item.id))

    logged = await WebhookDelivery.find(
        WebhookDelivery.subscription_id == "507f1f77bcf86cd799439011"
    ).to_list()
    assert len(logged) == 1
    assert logged[0].ok is False

    await item.delete()
    for d in logged:
        await d.delete()


# =========================================================================
# 7. One customer's payment webhook could resolve to another's payment
# =========================================================================


async def test_a_payment_webhook_only_resolves_its_own_tools_payments(client):
    """A reference is a string somebody else's system chose, and
    "order_1042" is entirely plausible for two providers to both produce.
    The signature proves the request came from the provider configured on
    THIS tool; it proves nothing about any other tool's payments."""
    import hashlib
    import hmac
    import json

    secret = "whsec_a"
    mine = BotTool(
        bot_id="bot-a", name="take_payment", description="Pay.", kind="http",
        method="POST", url="https://api.test/links",
        payment={
            "enabled": True,
            "webhook_secret_encrypted": encrypt_secret(secret),
            "webhook_reference_field": "ref",
            "webhook_status_field": "status",
            "webhook_paid_value": "paid",
        },
    )
    await mine.insert()

    # Somebody else's payment, carrying a colliding reference.
    theirs = PaymentSession(
        reference="order_1042", bot_id="bot-b", user_id="someone-else",
        tool_id="507f1f77bcf86cd799439011",
    )
    await theirs.insert()

    body = json.dumps({"ref": "order_1042", "status": "paid"}).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    resp = await client.post(
        f"/payments/webhook/{mine.id}",
        content=body,
        headers={"X-Razorpay-Signature": signature, "Content-Type": "application/json"},
    )

    assert resp.json()["status"] == "ignored"
    untouched = await PaymentSession.get(theirs.id)
    assert untouched.status == "pending", "another customer's payment was marked paid"

    await theirs.delete()
    await mine.delete()


async def test_a_settled_payment_is_forwarded_to_the_customers_own_webhooks(client):
    """A payment link is very often paid after the caller hung up, which
    is exactly when the live-call announcement has nobody to announce to.
    Without this the most important outcome in the flow was recorded here
    and nowhere the customer could see it."""
    import hashlib
    import hmac
    import json

    secret = "whsec_b"
    tool = BotTool(
        bot_id="bot-c", name="take_payment", description="Pay.", kind="http",
        method="POST", url="https://api.test/links",
        payment={
            "enabled": True,
            "webhook_secret_encrypted": encrypt_secret(secret),
            "webhook_reference_field": "ref",
            "webhook_status_field": "status",
            "webhook_paid_value": "paid",
        },
    )
    await tool.insert()
    sub = WebhookSubscription(
        user_id="user-pay", event="payment.received", url="https://example.com/hook",
        secret_encrypted=encrypt_secret("s"),
    )
    await sub.insert()
    session = PaymentSession(
        reference="pay_777", bot_id="bot-c", user_id="user-pay", tool_id=str(tool.id),
        amount="4200",
    )
    await session.insert()

    body = json.dumps({"ref": "pay_777", "status": "paid"}).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    await client.post(
        f"/payments/webhook/{tool.id}",
        content=body,
        headers={"X-Razorpay-Signature": signature, "Content-Type": "application/json"},
    )

    queued = await WebhookOutboxItem.find(
        WebhookOutboxItem.subscription_id == str(sub.id)
    ).to_list()
    assert len(queued) == 1
    assert queued[0].event == "payment.received"
    assert queued[0].payload["reference"] == "pay_777"

    for q in queued:
        await q.delete()
    await session.delete()
    await sub.delete()
    await tool.delete()


# =========================================================================
# 8. The call-scoped cache was being used where there is no call
# =========================================================================


async def test_a_lookup_outside_a_call_is_never_served_from_cache(monkeypatch):
    """The cache's entire safety argument is that a call gets its own OS
    process which then exits. The API process has no such boundary — it
    holds one module-level context for its whole life — so caching there
    meant the dashboard's Test button showing yesterday's answer after the
    customer fixed their API, and a dict that grew until restart."""
    client = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    call_context.clear()  # i.e. not in a call
    tool = _lookup_tool()

    await call_http_tool(tool, {})
    await call_http_tool(tool, {})

    assert client.calls == 2, "a result was cached outside a call"


async def test_a_lookup_inside_a_call_is_still_cached(monkeypatch):
    """Task 3.6's optimisation must survive: within one call, asking the
    same question twice still makes one request."""
    client = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    call_context.set_call(bot_id="bot-1", session_id="s-1", pc_id="pc-1")
    tool = _lookup_tool()

    await call_http_tool(tool, {})
    await call_http_tool(tool, {})

    assert client.calls == 1


# =========================================================================
# 9. Two tools with the same name
# =========================================================================


async def test_two_tools_with_the_same_name_do_not_both_reach_the_model():
    """A tool name becomes a function name in the schema sent to the
    provider, and that schema cannot express two functions with one name —
    the provider either rejects the request or silently keeps one, which
    presents as "my second tool never runs" with nothing in the logs."""
    from app.services.tool_registry import load_tools_for_bot

    a = BotTool(bot_id="bot-dupe", name="check_stock", description="A.", kind="http",
                method="GET", url="https://api.test/a")
    b = BotTool(bot_id="bot-dupe", name="check_stock", description="B.", kind="http",
                method="GET", url="https://api.test/b")
    await a.insert()
    await b.insert()

    tools, *_ = await load_tools_for_bot("bot-dupe")

    assert len({t.name for t in tools}) == len(tools)
    assert len(tools) == 1

    await a.delete()
    await b.delete()
