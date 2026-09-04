"""Task 3.8 — a customer's own routes onto their webhook subscriptions.

Every route here is scoped to the current user's own subscriptions, in
the same spirit as bot_tools.py: an id from someone else's account is
treated as not existing, not as a permission error, so nothing is leaked
about whether it exists at all.
"""

from datetime import UTC, datetime

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.crypto import decrypt_secret, encrypt_secret, mask_secret
from app.models.user import User
from app.models.webhook import EVENT_TYPES, WebhookDelivery, WebhookSubscription
from app.services.webhooks import deliver_now

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class SubscriptionIn(BaseModel):
    event: str
    url: str
    # Plain text on the way in only, like every other secret in this
    # codebase (see bot_tools.py's AuthIn.secret). None on update means
    # "keep the stored one."
    secret: str | None = None
    enabled: bool = True


def _out(sub: WebhookSubscription) -> dict:
    return {
        "id": str(sub.id),
        "event": sub.event,
        "url": sub.url,
        "enabled": sub.enabled,
        "secret_masked": mask_secret(decrypt_secret(sub.secret_encrypted)),
        "created_at": sub.created_at.isoformat(),
    }


async def _owned_subscription(sub_id: str, user_id: str) -> WebhookSubscription:
    try:
        sub = await WebhookSubscription.get(PydanticObjectId(sub_id))
    except Exception:
        sub = None
    if sub is None or sub.user_id != user_id:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return sub


@router.get("/events")
async def list_event_types():
    """What a subscription's `event` field is allowed to be — the manual's
    "define your event types" made visible to whoever is building the
    registration form, rather than left as tribal knowledge."""
    return {"events": sorted(EVENT_TYPES)}


@router.get("/")
async def list_subscriptions(current_user: User = Depends(get_current_user)):
    subs = await WebhookSubscription.find(WebhookSubscription.user_id == str(current_user.id)).to_list()
    return [_out(s) for s in subs]


@router.post("/", status_code=201)
async def create_subscription(body: SubscriptionIn, current_user: User = Depends(get_current_user)):
    if body.event not in EVENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"event must be one of {sorted(EVENT_TYPES)}",
        )
    sub = WebhookSubscription(
        user_id=str(current_user.id),
        event=body.event,
        url=body.url.strip(),
        enabled=body.enabled,
        secret_encrypted=encrypt_secret(body.secret or ""),
    )
    await sub.insert()
    return _out(sub)


@router.patch("/{sub_id}")
async def update_subscription(
    sub_id: str, body: SubscriptionIn, current_user: User = Depends(get_current_user)
):
    if body.event not in EVENT_TYPES:
        raise HTTPException(status_code=422, detail=f"event must be one of {sorted(EVENT_TYPES)}")
    sub = await _owned_subscription(sub_id, str(current_user.id))
    sub.event = body.event
    sub.url = body.url.strip()
    sub.enabled = body.enabled
    if body.secret is not None:
        sub.secret_encrypted = encrypt_secret(body.secret)
    await sub.save()
    return _out(sub)


@router.delete("/{sub_id}", status_code=204)
async def delete_subscription(sub_id: str, current_user: User = Depends(get_current_user)):
    sub = await _owned_subscription(sub_id, str(current_user.id))
    await sub.delete()


@router.get("/{sub_id}/deliveries")
async def list_deliveries(sub_id: str, current_user: User = Depends(get_current_user)):
    """The manual's own requirement: "show a delivery log so they can debug
    their end." Newest first — a customer checking this just fired a test
    or is chasing down why an event didn't arrive, and the answer is
    almost always at the top."""
    await _owned_subscription(sub_id, str(current_user.id))  # ownership check; not otherwise used
    deliveries = (
        await WebhookDelivery.find(WebhookDelivery.subscription_id == sub_id)
        .sort(-WebhookDelivery.created_at)  # type: ignore[arg-type]
        .limit(50)
        .to_list()
    )
    return [
        {
            "event": d.event,
            "attempt": d.attempt,
            "ok": d.ok,
            "status_code": d.status_code,
            "error": d.error,
            "created_at": d.created_at.isoformat(),
        }
        for d in deliveries
    ]


@router.post("/{sub_id}/test")
async def test_subscription(sub_id: str, current_user: User = Depends(get_current_user)):
    """Send one real, signed test event right now — no queue, no waiting
    for a retry schedule. The manual's own acceptance test for this task:
    "an event fires, reaches a test endpoint with a valid signature."

    Recorded as a real delivery (attempt 0, so it never collides with or
    counts against a queued item's own retry numbering) so it shows up in
    the same log a real event would, which is the whole point of testing
    against your own endpoint before relying on it.
    """
    sub = await _owned_subscription(sub_id, str(current_user.id))
    payload = {"note": "This is a test event from your Voice Agent dashboard.", "test": True}
    result = await deliver_now(sub, sub.event, payload)

    await WebhookDelivery(
        subscription_id=sub_id, event=sub.event, payload=payload, attempt=0,
        ok=result["ok"], status_code=result["status_code"], error=result["error"],
    ).insert()

    return result


@router.get("/_debug/outbox-summary")
async def outbox_summary(current_user: User = Depends(get_current_user)):
    """Not customer-facing — a quick operational check ("is anything stuck
    pending for me") that the dashboard's delivery log page can surface
    without a customer needing server access to ask the same question."""
    from app.models.webhook import WebhookOutboxItem

    subs = {
        str(s.id)
        for s in await WebhookSubscription.find(
            WebhookSubscription.user_id == str(current_user.id)
        ).to_list()
    }
    if not subs:
        return {"pending": 0, "failed": 0}

    items = await WebhookOutboxItem.find({"subscription_id": {"$in": list(subs)}}).to_list()
    # .replace(tzinfo=None): items came back from PyMongo naive (no
    # tz_aware=True on this client, even though every value here was
    # written as aware UTC) — comparing against an aware `now` directly
    # raises rather than answers, since Python refuses to order a naive
    # and an aware datetime against each other.
    now = datetime.now(UTC).replace(tzinfo=None)
    return {
        "pending": sum(1 for i in items if i.status == "pending"),
        "overdue": sum(1 for i in items if i.status == "pending" and i.next_attempt_at <= now),
        "failed": sum(1 for i in items if i.status == "failed"),
    }
