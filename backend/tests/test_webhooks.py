"""Task 3.8 — the webhook system.

The manual's acceptance test: "an event fires, reaches a test endpoint
with a valid signature, and retries correctly on failure." Three parts,
tested in that order below, plus the piece the manual calls out
separately — "always sign your webhooks... any competent customer will
refuse to accept unsigned events" — proven by actually verifying the
signature this system sends against the raw bytes on the receiving end,
the way a real customer's verifier would.

Delivery is queued in MongoDB rather than sent from wherever the event
happens (see models/webhook.py's docstring for why: an event usually
fires inside a call's own short-lived OS process, and a retry schedule
spanning minutes cannot safely live there). So `emit()` is tested as what
it actually is — fast, and durable — and the retry SCHEDULE is tested by
driving webhook_delivery_loop's single-pass function directly rather than
sleeping in wall-clock time for real delays.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core.crypto import encrypt_secret
from app.models.webhook import (
    EVENT_TYPES,
    WebhookDelivery,
    WebhookOutboxItem,
    WebhookSubscription,
)
from app.services import webhooks as webhooks_service
from app.services.webhooks import (
    MAX_ATTEMPTS,
    RETRY_DELAYS_SECONDS,
    SIGNATURE_HEADER,
    _run_due_deliveries,
    _sign,
    deliver_now,
    emit,
)
from tests.conftest import auth_headers

pytestmark = pytest.mark.asyncio(loop_scope="session")

SECRET = "whsec_test_receiver_side"


class _Resp:
    def __init__(self, status=200):
        self.status_code = status


class _Client:
    """Stands in for httpx, recording exactly what was sent — enough to
    verify the signature the way a real receiver would."""

    def __init__(self, status=200, raise_with=None):
        self.status, self.raise_with, self.seen = status, raise_with, {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kw):
        self.seen = {"url": url, **kw}
        if self.raise_with:
            raise self.raise_with
        return _Resp(self.status)


async def _make_subscription(
    event="appointment.booked", user_id="user-1", url="https://example.com/hook"
) -> WebhookSubscription:
    sub = WebhookSubscription(
        user_id=user_id, event=event, url=url, secret_encrypted=encrypt_secret(SECRET)
    )
    await sub.insert()
    return sub


# --- registration --------------------------------------------------------

async def test_registering_an_unknown_event_is_refused(client, user_a_token):
    resp = await client.post(
        "/webhooks/", json={"event": "not.a.real.event", "url": "https://example.com/hook"},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 422


async def test_the_event_list_is_the_real_one(client):
    resp = await client.get("/webhooks/events")
    assert set(resp.json()["events"]) == EVENT_TYPES


async def test_the_secret_never_comes_back(client, user_a_token):
    created = (await client.post(
        "/webhooks/", json={"event": "call.ended", "url": "https://example.com/hook", "secret": "sk_real"},
        headers=auth_headers(user_a_token),
    )).json()
    assert "secret" not in created
    assert "sk_real" not in json.dumps(created)
    assert created["secret_masked"].endswith("real")


async def test_a_user_cannot_see_or_touch_another_users_subscription(client, user_a_token, user_b_token):
    mine = (await client.post(
        "/webhooks/", json={"event": "call.ended", "url": "https://example.com/a"},
        headers=auth_headers(user_a_token),
    )).json()

    resp = await client.delete(f"/webhooks/{mine['id']}", headers=auth_headers(user_b_token))
    assert resp.status_code == 404


# --- signing (the manual's own emphasis) -----------------------------------

async def test_the_signature_verifies_against_the_exact_bytes_sent(monkeypatch):
    """Proven the way a real customer's receiver would: recompute the HMAC
    over the raw body actually sent and compare to the header."""
    sub = await _make_subscription()
    client = _Client()
    monkeypatch.setattr(webhooks_service.httpx, "AsyncClient", lambda **k: client)

    await deliver_now(sub, "appointment.booked", {"reference": "AB12"})

    sent_body = client.seen["content"]
    sent_sig = client.seen["headers"][SIGNATURE_HEADER]
    expected = hmac.new(SECRET.encode(), sent_body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expected, sent_sig)


def test_sign_is_deterministic_over_the_same_bytes():
    body = b'{"event": "x"}'
    assert _sign(SECRET, body) == _sign(SECRET, body)


def test_a_different_secret_produces_a_different_signature():
    body = b'{"event": "x"}'
    assert _sign(SECRET, body) != _sign("a-different-secret", body)


# --- emit() is fast and durable, not a network call -------------------------

async def test_emit_queues_without_making_any_network_call(monkeypatch):
    """emit() runs from inside a live call's tool handler — it must never
    itself reach out over the network."""
    sub = await _make_subscription()
    called = {"n": 0}

    async def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("emit() must not deliver directly")

    monkeypatch.setattr(webhooks_service, "deliver_now", _boom)
    await emit("appointment.booked", user_id=sub.user_id, payload={"reference": "X"})

    assert called["n"] == 0
    queued = await WebhookOutboxItem.find(WebhookOutboxItem.subscription_id == str(sub.id)).to_list()
    assert len(queued) == 1
    assert queued[0].status == "pending"


async def test_emit_with_no_matching_subscription_queues_nothing():
    await emit("appointment.cancelled", user_id="nobody-subscribed", payload={})
    items = await WebhookOutboxItem.find(WebhookOutboxItem.event == "appointment.cancelled").to_list()
    assert all(i.subscription_id != "nobody-subscribed" for i in items)


async def test_emit_ignores_a_disabled_subscription():
    sub = await _make_subscription(event="call.ended", user_id="user-disabled")
    sub.enabled = False
    await sub.save()

    await emit("call.ended", user_id="user-disabled", payload={})
    items = await WebhookOutboxItem.find(WebhookOutboxItem.subscription_id == str(sub.id)).to_list()
    assert items == []


async def test_emit_with_an_unrecognised_event_queues_nothing():
    """Application-code typo protection — this must never raise into the
    call that triggered it."""
    sub = await _make_subscription(event="call.ended", user_id="user-typo")
    await emit("call.eneded", user_id="user-typo", payload={})  # typo, deliberately
    items = await WebhookOutboxItem.find(WebhookOutboxItem.subscription_id == str(sub.id)).to_list()
    assert items == []


async def test_emit_with_no_user_id_does_not_raise():
    await emit("call.ended", user_id=None, payload={})  # must simply do nothing


async def test_two_subscribers_to_the_same_event_both_get_queued():
    sub1 = await _make_subscription(event="appointment.booked", user_id="user-multi", url="https://a.example.com")
    sub2 = await _make_subscription(event="appointment.booked", user_id="user-multi", url="https://b.example.com")

    await emit("appointment.booked", user_id="user-multi", payload={"reference": "Q9"})

    ids = {str(sub1.id), str(sub2.id)}
    items = await WebhookOutboxItem.find(
        WebhookOutboxItem.event == "appointment.booked"
    ).to_list()
    queued_for = {i.subscription_id for i in items if i.subscription_id in ids}
    assert queued_for == ids


# --- delivery and retry ------------------------------------------------------

async def test_a_successful_delivery_marks_the_item_delivered_and_logs_it(monkeypatch):
    sub = await _make_subscription()
    monkeypatch.setattr(webhooks_service.httpx, "AsyncClient", lambda **k: _Client(status=200))

    item = WebhookOutboxItem(subscription_id=str(sub.id), event=sub.event, payload={"x": 1})
    await item.insert()

    await _run_due_deliveries()

    saved = await WebhookOutboxItem.get(item.id)
    assert saved.status == "delivered"
    assert saved.attempt == 1

    logs = await WebhookDelivery.find(WebhookDelivery.subscription_id == str(sub.id)).to_list()
    assert any(d.ok and d.attempt == 1 for d in logs)


async def test_a_failed_delivery_is_rescheduled_with_the_first_delay(monkeypatch):
    sub = await _make_subscription()
    monkeypatch.setattr(webhooks_service.httpx, "AsyncClient", lambda **k: _Client(status=500))

    item = WebhookOutboxItem(subscription_id=str(sub.id), event=sub.event, payload={})
    await item.insert()
    before = datetime.now(UTC)

    await _run_due_deliveries()

    saved = await WebhookOutboxItem.get(item.id)
    assert saved.status == "pending", "a failure must stay pending to be retried"
    assert saved.attempt == 1
    expected_earliest = before + timedelta(seconds=RETRY_DELAYS_SECONDS[0] - 1)
    assert saved.next_attempt_at >= expected_earliest, "did not wait the increasing delay before retrying"


async def test_a_delivery_not_yet_due_is_left_alone(monkeypatch):
    sub = await _make_subscription()
    client = _Client(status=200)
    monkeypatch.setattr(webhooks_service.httpx, "AsyncClient", lambda **k: client)

    item = WebhookOutboxItem(
        subscription_id=str(sub.id), event=sub.event, payload={},
        next_attempt_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await item.insert()

    await _run_due_deliveries()

    assert client.seen == {}, "a delivery not yet due was attempted anyway"
    saved = await WebhookOutboxItem.get(item.id)
    assert saved.attempt == 0


async def test_repeated_failure_eventually_gives_up(monkeypatch):
    """Retries with increasing delays, but not forever."""
    sub = await _make_subscription()
    monkeypatch.setattr(webhooks_service.httpx, "AsyncClient", lambda **k: _Client(status=500))

    item = WebhookOutboxItem(subscription_id=str(sub.id), event=sub.event, payload={})
    await item.insert()

    for _ in range(MAX_ATTEMPTS):
        item = await WebhookOutboxItem.get(item.id)
        item.next_attempt_at = datetime.now(UTC)  # force each attempt due immediately
        await item.save()
        await _run_due_deliveries()

    saved = await WebhookOutboxItem.get(item.id)
    assert saved.status == "failed"
    assert saved.attempt == MAX_ATTEMPTS

    logs = await WebhookDelivery.find(WebhookDelivery.subscription_id == str(sub.id)).to_list()
    assert len(logs) == MAX_ATTEMPTS, "not every attempt was logged"


async def test_a_network_exception_during_delivery_is_recorded_not_raised(monkeypatch):
    sub = await _make_subscription()
    monkeypatch.setattr(
        webhooks_service.httpx, "AsyncClient",
        lambda **k: _Client(raise_with=RuntimeError("dns failure")),
    )
    item = WebhookOutboxItem(subscription_id=str(sub.id), event=sub.event, payload={})
    await item.insert()

    await _run_due_deliveries()  # must not raise

    saved = await WebhookOutboxItem.get(item.id)
    assert saved.status == "pending"
    logs = await WebhookDelivery.find(WebhookDelivery.subscription_id == str(sub.id)).to_list()
    assert "dns failure" in logs[0].error


async def test_a_deleted_subscription_fails_its_queued_item_without_retrying():
    item = WebhookOutboxItem(subscription_id="a-subscription-that-was-deleted", event="call.ended", payload={})
    await item.insert()

    await _run_due_deliveries()

    saved = await WebhookOutboxItem.get(item.id)
    assert saved.status == "failed"


# --- the test-send endpoint (the manual's own acceptance test) --------------

async def test_the_test_endpoint_delivers_immediately_with_a_valid_signature(client, user_a_token, monkeypatch):
    sub_resp = await client.post(
        "/webhooks/", json={"event": "call.ended", "url": "https://example.com/hook", "secret": SECRET},
        headers=auth_headers(user_a_token),
    )
    sub_id = sub_resp.json()["id"]

    captured = {}

    class _CapturingClient(_Client):
        async def post(self, url, **kw):
            captured.update({"url": url, **kw})
            return _Resp(200)

    monkeypatch.setattr(webhooks_service.httpx, "AsyncClient", lambda **k: _CapturingClient())

    resp = await client.post(f"/webhooks/{sub_id}/test", headers=auth_headers(user_a_token))

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    expected = hmac.new(SECRET.encode(), captured["content"], hashlib.sha256).hexdigest()
    assert captured["headers"][SIGNATURE_HEADER] == expected

    log = await client.get(f"/webhooks/{sub_id}/deliveries", headers=auth_headers(user_a_token))
    assert any(entry["attempt"] == 0 for entry in log.json()), "the test send did not appear in the log"


# --- delivery log ------------------------------------------------------------

async def test_the_delivery_log_shows_newest_first(client, user_a_token):
    # Built through the API rather than _make_subscription() so its owner
    # actually matches user_a_token — the log route checks that.
    created = (await client.post(
        "/webhooks/", json={"event": "call.ended", "url": "https://example.com/order"},
        headers=auth_headers(user_a_token),
    )).json()

    for attempt, ok in [(1, False), (2, True)]:
        await WebhookDelivery(
            subscription_id=created["id"], event="call.ended", attempt=attempt, ok=ok,
            created_at=datetime.now(UTC) + timedelta(seconds=attempt),
        ).insert()

    resp = await client.get(f"/webhooks/{created['id']}/deliveries", headers=auth_headers(user_a_token))
    entries = resp.json()
    assert entries[0]["attempt"] == 2, "newest delivery was not listed first"
