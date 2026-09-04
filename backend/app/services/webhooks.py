"""Task 3.8 — telling a customer's own system what just happened here.

The manual's reason: customers want events flowing into their own tools,
and a webhook is the universal way to do that without a bespoke
integration per customer. See models/webhook.py for why delivery is
queued in MongoDB rather than attempted directly where the event happens.

The manual's five steps, and where each one lives:

  - event types and payloads: EVENT_TYPES in models/webhook.py, and the
    payload each call site already builds (see booking.py's `_emit`).
  - customers register a URL per event: app/api/webhooks.py, one
    WebhookSubscription per (user, event) pair.
  - sign every request: `_sign` below, HMAC-SHA256 over the exact bytes
    sent — the same scheme task 3.7 verifies on the way IN, used here on
    the way OUT, so a customer's own verifier is the mirror of this
    system's inbound one.
    it. NOT a claim that this is universal — see task 3.7's identical
    caveat — but it is a documented, common, and verifiable scheme, not a
    guess.
  - retry with increasing delays: RETRY_DELAYS_SECONDS, applied by
    webhook_delivery_loop.
  - a delivery log: every attempt, successful or not, becomes a
    WebhookDelivery row — see app/api/webhooks.py's log endpoint.
"""

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
from beanie import PydanticObjectId
from loguru import logger

from app.core.crypto import decrypt_secret
from app.models.webhook import (
    EVENT_TYPES,
    WebhookDelivery,
    WebhookOutboxItem,
    WebhookSubscription,
)

# Increasing, as the manual asks — a customer's endpoint that is briefly
# down (a deploy, a cold start) is far more common than one that is down
# for good, and space between attempts gives exactly that kind of blip room
# to resolve itself before this gives up. One initial attempt (in the
# background loop's first pass) plus these three retries: 4 tries total,
# spread over a little over two minutes end to end.
RETRY_DELAYS_SECONDS = [5, 30, 120]
MAX_ATTEMPTS = len(RETRY_DELAYS_SECONDS) + 1

# Bounded so one slow or hanging customer endpoint cannot back up delivery
# for every other subscriber's events behind it in the same poll.
DELIVERY_TIMEOUT_SECONDS = 8.0

SIGNATURE_HEADER = "X-Voiceagent-Signature"
EVENT_HEADER = "X-Voiceagent-Event"


def _sign(secret: str, body: bytes) -> str:
    """HMAC-SHA256 of the exact bytes sent — signing anything else (a
    re-serialised dict, say) risks the customer's verifier computing a
    different signature over what looks like the same data."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def emit(event: str, user_id: str | None, payload: dict) -> None:
    """Queue `event` for every enabled subscription that wants it.

    Fast and durable, deliberately: this returns after one insert per
    subscriber and does not wait for any of them to actually be delivered.
    Called from inside a live call's tool handler (see booking.py) — it
    must never make a caller wait on a customer's webhook endpoint, and it
    must survive the call's own process exiting shortly after.

    A blank user_id or an event outside EVENT_TYPES queues nothing rather
    than raising: the caller here is always application code with a typo
    risk of its own, and a broken webhook must never break the feature that
    triggered it.
    """
    if not user_id:
        logger.warning(f"[WEBHOOK] emit({event!r}) with no user_id — nothing queued")
        return
    if event not in EVENT_TYPES:
        logger.warning(f"[WEBHOOK] emit({event!r}): not a recognised event type — nothing queued")
        return

    try:
        subs = await WebhookSubscription.find(
            WebhookSubscription.user_id == user_id,
            WebhookSubscription.event == event,
            WebhookSubscription.enabled == True,  # noqa: E712
        ).to_list()
    except Exception as e:
        logger.warning(f"[WEBHOOK] Could not look up subscriptions for {event!r}: {type(e).__name__}: {e}")
        return

    for sub in subs:
        try:
            await WebhookOutboxItem(
                subscription_id=str(sub.id), event=event, payload=payload
            ).insert()
        except Exception as e:
            logger.warning(f"[WEBHOOK] Could not queue {event!r} for subscription {sub.id}: {e}")

    if subs:
        logger.info(f"[WEBHOOK] Queued {event!r} for {len(subs)} subscription(s)")


async def deliver_now(sub: WebhookSubscription, event: str, payload: dict) -> dict:
    """One delivery attempt, right now, no queue and no retry.

    Used by the "send a test event" endpoint — a customer wiring this up
    wants to know immediately whether their endpoint is reachable and
    their secret matches, not after this attempt happens to be due. Also
    the actual delivery mechanism webhook_delivery_loop calls per attempt;
    the queue decides WHEN, this decides WHAT HAPPENS.
    """
    secret = decrypt_secret(sub.secret_encrypted)
    body = json.dumps(
        {"event": event, "payload": payload, "sent_at": datetime.now(UTC).isoformat()},
        default=str,
    ).encode()
    signature = _sign(secret, body)

    try:
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS) as client:
            response = await client.post(
                sub.url, content=body,
                headers={
                    "Content-Type": "application/json",
                    SIGNATURE_HEADER: signature,
                    EVENT_HEADER: event,
                },
            )
        ok = response.status_code < 400
        return {"ok": ok, "status_code": response.status_code, "error": "" if ok else f"HTTP {response.status_code}"}
    except Exception as e:
        return {"ok": False, "status_code": None, "error": f"{type(e).__name__}: {e}"}


async def webhook_delivery_loop(interval_seconds: int = 5) -> None:
    """Background loop (started from main.py's lifespan) — works through
    whatever is due in the outbox, retrying failures with the delays above.

    Polls rather than reacting instantly to a new queue item: this is
    MongoDB doing duty as a simple durable queue, not a message broker with
    push semantics, and a several-second delay before the FIRST attempt is
    an acceptable trade for not needing to run one — the manual's own
    "Redis queue" suggestion is exactly that trade in the other direction,
    and is the natural next step if delivery volume ever demands it.
    """
    while True:
        await _run_due_deliveries()
        await asyncio.sleep(interval_seconds)


async def _run_due_deliveries() -> None:
    try:
        due = await WebhookOutboxItem.find(
            WebhookOutboxItem.status == "pending",
            WebhookOutboxItem.next_attempt_at <= datetime.now(UTC),
        ).to_list()
    except Exception as e:
        logger.warning(f"[WEBHOOK] Could not read the outbox: {type(e).__name__}: {e}")
        return

    if not due:
        return

    # Concurrent, not sequential: one customer's slow endpoint (up to
    # DELIVERY_TIMEOUT_SECONDS each) must not delay every other pending
    # delivery behind it in the same pass.
    await asyncio.gather(*(_process_one(item) for item in due), return_exceptions=True)


async def _process_one(item: WebhookOutboxItem) -> None:
    try:
        sub = await WebhookSubscription.get(PydanticObjectId(item.subscription_id))
    except Exception:
        sub = None

    if sub is None or not sub.enabled:
        # The subscription was deleted or disabled since this was queued —
        # nowhere left to send it, and not a failure worth retrying.
        item.status = "failed"
        await item.save()
        return

    item.attempt += 1
    result = await deliver_now(sub, item.event, item.payload)

    await WebhookDelivery(
        subscription_id=item.subscription_id, event=item.event, payload=item.payload,
        attempt=item.attempt, ok=result["ok"], status_code=result["status_code"],
        error=result["error"],
    ).insert()

    if result["ok"]:
        item.status = "delivered"
        logger.info(
            f"[WEBHOOK] Delivered {item.event!r} to subscription {item.subscription_id} "
            f"(attempt {item.attempt})"
        )
    elif item.attempt >= MAX_ATTEMPTS:
        item.status = "failed"
        logger.warning(
            f"[WEBHOOK] Giving up on {item.event!r} for subscription {item.subscription_id} "
            f"after {item.attempt} attempts: {result['error']}"
        )
    else:
        delay = RETRY_DELAYS_SECONDS[item.attempt - 1]
        item.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
        logger.info(
            f"[WEBHOOK] {item.event!r} to subscription {item.subscription_id} failed "
            f"(attempt {item.attempt}/{MAX_ATTEMPTS}): {result['error']} — retrying in {delay}s"
        )

    await item.save()
