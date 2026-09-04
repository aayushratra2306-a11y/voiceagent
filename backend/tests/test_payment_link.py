"""Task 3.7 — the payment link tool.

Generating a link and texting it needs no code of its own: task 3.1's
generic HTTP tool already does both against whatever provider a customer
uses. What this task adds — and what these tests pin — is the part
configuration cannot do: an asynchronous "it was paid" from the provider
finding its way back into the specific conversation that asked for it.

The tests are ordered by how much damage getting each wrong would do:

  1. An unsigned or wrongly-signed webhook changes nothing and tells the
     caller nothing. This route is necessarily unauthenticated (a payment
     provider holds no token of ours), so the signature is the only thing
     standing between a stranger with the URL and a caller being told
     their payment succeeded.
  2. A verified webhook is RECORDED even when nobody can hear it — the
     call may have ended while the caller was paying.
  3. Only then: it reaches the live call.

Nothing here moves money, and nothing in the feature does either. The
tool records what a provider reports and repeats it; card details never
touch this system at all, which is what PAYMENT_SAFETY_RULE tells the bot.
"""

import hashlib
import hmac
import json

import pytest

from app.core.crypto import encrypt_secret
from app.models.bot_tool import BotTool, PaymentLinkConfig
from app.models.payment import PaymentSession
from app.pipeline import call_context
from app.services import tool_registry
from app.services.tool_registry import PAYMENT_SAFETY_RULE, call_http_tool

pytestmark = pytest.mark.asyncio(loop_scope="session")

SECRET = "whsec_test_not_a_real_key"


class _Resp:
    def __init__(self, status=200, text='{"id": "plink_123", "short_url": "https://rzp.io/i/abc", "amount": "500"}'):
        self.status_code, self.text = status, text


class _Client:
    def __init__(self, response=None):
        self.response = response or _Resp()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kw):
        return self.response


def _payment_tool(**over) -> BotTool:
    payment = PaymentLinkConfig(
        enabled=True,
        reference_field="id",
        amount_field="amount",
        link_field="short_url",
        webhook_secret_encrypted=encrypt_secret(SECRET),
        webhook_reference_field="payload.payment_link.entity.id",
        webhook_status_field="payload.payment_link.entity.status",
        webhook_paid_value="paid",
    )
    base = dict(
        bot_id="bot-pay", name="send_payment_link",
        description="Create a payment link for the caller.",
        kind="http", method="POST", url="https://api.test/payment_links",
        payment=payment,
    )
    base.update(over)
    return BotTool(**base)


def _signed(body: dict, secret: str = SECRET) -> tuple[bytes, str]:
    raw = json.dumps(body).encode()
    return raw, hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _webhook_body(reference: str, status: str = "paid") -> dict:
    return {"payload": {"payment_link": {"entity": {"id": reference, "status": status}}}}


@pytest.fixture(autouse=True)
def _clean_context():
    call_context.clear()
    yield
    call_context.clear()


# --- 1. nothing is trusted before the signature ----------------------------

async def test_an_unsigned_webhook_is_rejected_and_records_nothing(client):
    tool = _payment_tool()
    await tool.insert()
    session = PaymentSession(reference="plink_unsigned", bot_id="b", tool_id=str(tool.id))
    await session.insert()

    body = _webhook_body("plink_unsigned")
    resp = await client.post(f"/payments/webhook/{tool.id}", json=body)   # no signature header

    assert resp.status_code == 400
    unchanged = await PaymentSession.find_one(PaymentSession.reference == "plink_unsigned")
    assert unchanged.status == "pending", "an unsigned request changed a payment's status"


async def test_a_wrongly_signed_webhook_is_rejected(client):
    tool = _payment_tool()
    await tool.insert()
    session = PaymentSession(reference="plink_wrong", bot_id="b", tool_id=str(tool.id))
    await session.insert()

    raw, _ = _signed(_webhook_body("plink_wrong"), secret="someone-elses-secret")
    resp = await client.post(
        f"/payments/webhook/{tool.id}",
        content=raw,
        headers={"X-Razorpay-Signature": "deadbeef", "Content-Type": "application/json"},
    )

    assert resp.status_code == 400
    unchanged = await PaymentSession.find_one(PaymentSession.reference == "plink_wrong")
    assert unchanged.status == "pending"


async def test_an_unknown_tool_id_gives_the_same_answer_as_a_bad_signature(client):
    """A stranger probing this endpoint should not be able to tell which
    tool ids exist from the response."""
    from beanie import PydanticObjectId

    resp = await client.post(f"/payments/webhook/{PydanticObjectId()}", json={})
    assert resp.status_code == 400
    assert "Could not process" in resp.json()["detail"]


def test_signature_verification_itself():
    from app.api.payments import _verify_signature

    raw, sig = _signed(_webhook_body("x"))
    assert _verify_signature(SECRET, raw, sig) is True
    assert _verify_signature(SECRET, raw, "nope") is False
    assert _verify_signature("", raw, sig) is False, "no configured secret must never verify"
    assert _verify_signature(SECRET, raw, "") is False


# --- 2. a verified payment is recorded, heard or not -----------------------

async def test_a_verified_webhook_marks_the_payment_paid(client):
    tool = _payment_tool()
    await tool.insert()
    await PaymentSession(reference="plink_ok", bot_id="b", tool_id=str(tool.id)).insert()

    raw, sig = _signed(_webhook_body("plink_ok", "paid"))
    resp = await client.post(
        f"/payments/webhook/{tool.id}", content=raw,
        headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
    )

    assert resp.status_code == 200
    saved = await PaymentSession.find_one(PaymentSession.reference == "plink_ok")
    assert saved.status == "paid"
    assert saved.resolved_at is not None
    assert saved.last_webhook, "the provider's own payload was not kept for debugging"


async def test_a_status_that_is_not_the_paid_value_is_recorded_as_failed(client):
    tool = _payment_tool()
    await tool.insert()
    await PaymentSession(reference="plink_bad", bot_id="b", tool_id=str(tool.id)).insert()

    raw, sig = _signed(_webhook_body("plink_bad", "expired"))
    await client.post(
        f"/payments/webhook/{tool.id}", content=raw,
        headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
    )

    saved = await PaymentSession.find_one(PaymentSession.reference == "plink_bad")
    assert saved.status == "failed", "anything but the configured paid value must not read as paid"


async def test_a_payment_for_an_ended_call_is_still_recorded(client):
    """The caller hung up while paying. There is nobody to tell — the
    payment still happened and must not be lost."""
    tool = _payment_tool()
    await tool.insert()
    # pc_id points at a call that is not in the live registry.
    await PaymentSession(
        reference="plink_gone", bot_id="b", tool_id=str(tool.id), pc_id="a-call-that-ended"
    ).insert()

    raw, sig = _signed(_webhook_body("plink_gone"))
    resp = await client.post(
        f"/payments/webhook/{tool.id}", content=raw,
        headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
    )

    assert resp.json()["status"] == "recorded"
    saved = await PaymentSession.find_one(PaymentSession.reference == "plink_gone")
    assert saved.status == "paid"


async def test_a_verified_webhook_for_an_unknown_reference_is_ignored_quietly(client):
    tool = _payment_tool()
    await tool.insert()

    raw, sig = _signed(_webhook_body("a-reference-we-never-issued"))
    resp = await client.post(
        f"/payments/webhook/{tool.id}", content=raw,
        headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


# --- 3. it reaches the live call -------------------------------------------

async def test_a_paid_webhook_is_pushed_to_the_live_call(client, monkeypatch):
    tool = _payment_tool()
    await tool.insert()
    await PaymentSession(
        reference="plink_live", bot_id="b", tool_id=str(tool.id), pc_id="pc-live", amount="500"
    ).insert()

    pushed = []

    class _Q:
        def put(self, item):
            pushed.append(item)

    monkeypatch.setattr("app.api.payments.get_payment_queue", lambda pc_id: _Q() if pc_id == "pc-live" else None)

    raw, sig = _signed(_webhook_body("plink_live"))
    resp = await client.post(
        f"/payments/webhook/{tool.id}", content=raw,
        headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
    )

    assert resp.json()["status"] == "announced"
    assert pushed == [{"reference": "plink_live", "status": "paid", "amount": "500"}]


async def test_a_queue_failure_does_not_lose_the_payment(client, monkeypatch):
    """Reaching the call is best-effort; the record is not."""
    tool = _payment_tool()
    await tool.insert()
    await PaymentSession(
        reference="plink_qfail", bot_id="b", tool_id=str(tool.id), pc_id="pc-live"
    ).insert()

    class _BrokenQ:
        def put(self, item):
            raise RuntimeError("pipe is gone")

    monkeypatch.setattr("app.api.payments.get_payment_queue", lambda pc_id: _BrokenQ())

    raw, sig = _signed(_webhook_body("plink_qfail"))
    resp = await client.post(
        f"/payments/webhook/{tool.id}", content=raw,
        headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
    )

    assert resp.json()["status"] == "recorded"
    saved = await PaymentSession.find_one(PaymentSession.reference == "plink_qfail")
    assert saved.status == "paid"


# --- creating the link tracks it against this call -------------------------

async def test_creating_a_link_records_it_against_the_current_call(monkeypatch):
    tool = _payment_tool()
    await tool.insert()
    call_context.set_call(bot_id="bot-pay", session_id="s1", pc_id="pc-42")
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: _Client())

    result = await call_http_tool(tool, {})

    assert result["ok"] is True
    saved = await PaymentSession.find_one(PaymentSession.reference == "plink_123")
    assert saved is not None, "the link was created but nothing was tracked"
    assert saved.pc_id == "pc-42", "a webhook could never find this call"
    assert saved.link_url == "https://rzp.io/i/abc"
    assert saved.status == "pending"
    assert "paid" in result["message"].lower()


async def test_a_link_whose_reference_cannot_be_found_still_succeeds(monkeypatch):
    """The link itself was created — the caller's actual request worked.
    Only the automatic confirmation is lost, and the model is told so."""
    tool = _payment_tool()
    tool.payment.reference_field = "not.a.real.path"
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: _Client())

    result = await call_http_tool(tool, {})

    assert result["ok"] is True, "a tracking problem must not fail the payment link"
    assert "cannot confirm automatically" in result["message"]


async def test_a_payment_link_call_is_never_served_from_the_cache(monkeypatch):
    """Creating a link is a side effect — replaying a cached one would hand
    the caller a stale link and a reference that belongs to an older
    request."""
    calls = {"n": 0}

    class _Counting(_Client):
        async def request(self, method, url, **kw):
            calls["n"] += 1
            return self.response

    tool = _payment_tool(method="GET")  # even as a GET, which is what the cache keys on
    call_context.set_call(bot_id="bot-pay", session_id="s1", pc_id="pc-1")
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: _Counting())

    await call_http_tool(tool, {})
    await call_http_tool(tool, {})

    assert calls["n"] == 2, "a payment link was served from the cache"


# --- the safety rule -------------------------------------------------------

def test_the_bot_is_told_never_to_take_card_details():
    """The manual's tip on this task: never take card numbers by voice.
    The compliance burden is enormous and completely avoidable."""
    rule = PAYMENT_SAFETY_RULE.lower()
    assert "never ask" in rule
    assert "card number" in rule and "cvv" in rule
    assert "link" in rule


def test_the_safety_rule_is_only_added_for_a_bot_with_a_payment_tool():
    import inspect

    from app.pipeline import voice_pipeline

    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "if has_payment:" in source
    assert "PAYMENT_SAFETY_RULE" in source


def test_the_pipeline_can_be_reached_by_a_payment_update():
    """The cross-process channel exists and is wired to the announcer."""
    import inspect

    from app.pipeline import voice_pipeline

    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "_forward_payments" in source
    assert "announce_external" in source
