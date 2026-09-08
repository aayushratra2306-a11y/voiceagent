"""Task 3.7 — tracking one payment link against the live call it belongs to.

The manual's hard part for this task isn't generating a link or sending it
by SMS — task 3.1's generic HTTP tool already does both of those with zero
new code, configured against whatever provider a customer actually uses.
The hard part is the step after: "listen for the payment callback and
confirm on the call if it lands." That is inherently asynchronous — the
caller may still be talking, on hold, or already hung up by the time the
payment provider tells us what happened — and this record is what makes
the callback able to find its way back to the right conversation.

`pc_id` is the field that matters: it is the same id `app.api.connect`
keys its live-call registry by, so a webhook arriving seconds or minutes
after the link was sent can look up whether that specific call is still
open and, if so, speak the result into it. If the call has already ended,
the payment still gets recorded here — just with nobody to tell.
"""

from datetime import UTC, datetime
from typing import Literal

from beanie import Document
from pydantic import Field


class PaymentSession(Document):
    reference: str  # what the provider's webhook will report back
    bot_id: str
    # Which live call this belongs to, so a webhook arriving later can find
    # it. Not the caller's identity — just the process-registry key task
    # 2.4's connect.py already uses. Blank is possible (the tool could in
    # principle run outside a call context) and means "nobody to tell."
    pc_id: str = ""
    tool_id: str  # which BotTool's webhook secret verifies this reference
    # The bot's owner. Carried so the provider's callback can be forwarded
    # on to that customer's own webhook subscriptions (task 3.8) — without
    # it, a payment landing after the caller hung up is recorded here and
    # nowhere else, which is the case a customer most needs told about.
    user_id: str = ""

    amount: str = ""
    currency: str = ""
    link_url: str = ""

    status: Literal["pending", "paid", "failed"] = "pending"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None
    # The webhook's own payload, kept for whoever has to debug a payment
    # that the customer says arrived but the call never heard about.
    last_webhook: dict = Field(default_factory=dict)

    class Settings:
        name = "payment_sessions"
