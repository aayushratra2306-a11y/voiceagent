"""Task 3.7 — the inbound half of the payment link tool.

Generating a link and sending it by SMS or WhatsApp needs no code here:
task 3.1's generic HTTP tool already does both, configured against whatever
provider and messaging service a customer actually uses. What could not be
done by configuration alone is this — the provider calling US back, minutes
later, to say the money arrived, and that news finding its way into a
conversation that may still be in progress.

Three things this route is careful about, in order of how much damage
getting them wrong would do:

  1. **Nothing is trusted before the signature is verified.** A caller
     being told "your payment went through" on the strength of an unsigned
     HTTP request is the whole reason webhook signing exists. An
     unverifiable request is rejected before the reference is even read.

  2. **The route is unauthenticated by necessity, so it must be safe when
     abused.** A payment provider has no bearer token from us. That means
     anyone on the internet can POST here, and every path through this
     code has to end in a boring answer rather than an information leak —
     the same 400 whether the signature was wrong, the reference unknown,
     or the tool has no secret configured at all.

  3. **A payment is recorded whether or not anybody hears it.** The call
     may have ended thirty seconds ago. The PaymentSession row is updated
     regardless; announcing into the live call is best-effort on top.

On money, deliberately: this route only ever RECORDS what a provider says
happened and repeats it to the caller. It never moves money, never
initiates a charge, and never sees a card number — the manual's tip on
this task is that card data should never touch this system at all, and
`PAYMENT_SAFETY_RULE` (in tool_registry) is what tells the bot the same.
"""

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId
from fastapi import APIRouter, HTTPException, Request
from loguru import logger

from app.api.connect import get_payment_queue
from app.core.crypto import decrypt_secret
from app.models.bot_tool import BotTool
from app.models.payment import PaymentSession
from app.services.tool_registry import _resolve_path

router = APIRouter(prefix="/payments", tags=["payments"])


def _verify_signature(secret: str, raw_body: bytes, provided: str) -> bool:
    """HMAC-SHA256 of the raw body, compared in constant time.

    This is Razorpay's documented scheme, and a common one. It is NOT
    universal — Stripe, for one, signs a timestamp-prefixed payload and
    needs its own verifier. A provider that signs differently must not be
    pointed at this endpoint and told it works; it needs its own, which is
    why the scheme is named here rather than left implied.

    compare_digest rather than `==` because a plain comparison returns as
    soon as two bytes differ, and the time it took to say no is itself a
    clue to what the right answer would have been.
    """
    if not secret or not provided:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.strip())


async def _forward_to_customer(session: PaymentSession, paid: bool) -> None:
    """Pass a settled payment on to the customer's own webhooks (task 3.8).

    Wrapped, like every other emit() in this codebase: this route's job is
    to record what the provider said and answer it promptly. A failure to
    notify must not turn into a non-2xx back to the payment provider,
    which would make it retry a webhook that was, in fact, processed
    correctly.

    Nothing is sent for a session created before this field existed
    (user_id blank) — emit() logs and drops it rather than guessing whose
    payment it was.
    """
    try:
        from app.services.webhooks import emit

        await emit(
            "payment.received" if paid else "payment.failed",
            user_id=session.user_id,
            payload={
                "reference": session.reference,
                "status": session.status,
                "amount": session.amount,
                "currency": session.currency,
                "bot_id": session.bot_id,
                "link_url": session.link_url,
            },
        )
    except Exception as e:
        logger.warning(f"[PAYMENT] Could not queue a notification for {session.reference}: {e}")


@router.post("/webhook/{tool_id}")
async def payment_webhook(tool_id: str, request: Request) -> dict[str, Any]:
    """Receive a payment provider's callback for one configured tool.

    Deliberately keyed by tool rather than by bot or by payment: the tool
    record is what holds the webhook secret and the paths into this
    provider's particular JSON shape, and it is the only thing the provider
    can be configured to send us that identifies which customer's
    integration this is.
    """
    # The RAW bytes, before any parsing — a signature is over exactly what
    # was sent, and re-serialising parsed JSON would change whitespace and
    # key order and never match.
    raw_body = await request.body()

    try:
        tool = await BotTool.get(PydanticObjectId(tool_id))
    except Exception:
        tool = None
    if tool is None or not tool.payment.enabled:
        # Same answer as a bad signature: someone probing this endpoint
        # learns nothing about which tool ids exist.
        logger.warning(f"[PAYMENT] Webhook for unknown/non-payment tool {tool_id}")
        raise HTTPException(status_code=400, detail="Could not process this webhook")

    secret = decrypt_secret(tool.payment.webhook_secret_encrypted)
    signature = request.headers.get(tool.payment.signature_header, "")
    if not _verify_signature(secret, raw_body, signature):
        logger.warning(
            f"[PAYMENT] Rejected an unverified webhook for tool {tool.name} "
            f"(header {tool.payment.signature_header!r}) — nothing was recorded"
        )
        raise HTTPException(status_code=400, detail="Could not process this webhook")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Could not process this webhook") from None

    reference = _resolve_path(payload, tool.payment.webhook_reference_field)
    raw_status = _resolve_path(payload, tool.payment.webhook_status_field)
    if not reference:
        logger.warning(
            f"[PAYMENT] Verified webhook for {tool.name} had no reference at "
            f"{tool.payment.webhook_reference_field!r} — check that path against "
            f"what the provider actually sends"
        )
        raise HTTPException(status_code=400, detail="Could not process this webhook")

    # Scoped to THIS tool, not to the reference alone. A reference is a
    # string somebody else's system chose — "order_1042" is an entirely
    # plausible thing for two different customers' providers to both
    # produce — and a global lookup by reference would let one customer's
    # verified webhook resolve to, and mark paid, another customer's
    # payment session. The signature check above proves the request came
    # from the provider configured on THIS tool; it proves nothing about
    # any other tool's payments, so the query says so.
    session = await PaymentSession.find_one(
        PaymentSession.reference == str(reference),
        PaymentSession.tool_id == str(tool.id),
    )
    if session is None:
        # Verified, so this is a real message from a real provider — it just
        # doesn't match a link this system created. Worth a log, not an error.
        logger.warning(f"[PAYMENT] No tracked payment for reference {reference}")
        return {"status": "ignored"}

    paid = str(raw_status) == tool.payment.webhook_paid_value
    session.status = "paid" if paid else "failed"
    session.resolved_at = datetime.now(UTC)
    session.last_webhook = payload if isinstance(payload, dict) else {"raw": str(payload)}
    await session.save()
    logger.info(f"[PAYMENT] {session.reference} -> {session.status}")

    # Task 3.8 doing for this what it does for every other event. The live
    # announcement below only reaches a caller who is still on the line,
    # and a payment link is very often paid minutes after the call ended —
    # so without this, the single most important outcome in the whole
    # payment flow is the one the customer's own systems never hear about.
    # Queued (not sent) here, so a customer's slow endpoint cannot delay
    # answering the payment provider, which retries if we are slow to 200.
    await _forward_to_customer(session, paid)

    # Best-effort, and last: the record above is what must not be lost. If
    # the call ended while the caller was paying, there is simply nobody to
    # tell — that is a normal ending, not a failure.
    queue = get_payment_queue(session.pc_id) if session.pc_id else None
    if queue is None:
        logger.info(f"[PAYMENT] Call for {session.reference} is no longer live; nobody told")
        return {"status": "recorded"}

    try:
        queue.put({
            "reference": session.reference,
            "status": session.status,
            "amount": session.amount,
        })
    except Exception as e:
        logger.warning(f"[PAYMENT] Could not reach the live call: {type(e).__name__}: {e}")
        return {"status": "recorded"}

    return {"status": "announced"}
