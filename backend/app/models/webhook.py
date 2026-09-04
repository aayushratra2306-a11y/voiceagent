"""Task 3.8 — telling a customer's own system what just happened here.

The manual's reason for this existing at all: customers want events
flowing into their own tools, and a webhook is the universal way to do
that without a bespoke integration per customer. It is the outbound
counterpart to task 3.7's inbound one — there, a payment provider tells
US something happened; here, THIS system tells a customer's own service
something happened, in the same signed-and-verifiable spirit.

Two documents:

  WebhookSubscription — one URL a customer wants told about one event
    type. A customer registers as many of these as they have events they
    care about; nothing is sent anywhere until at least one exists.

  WebhookDelivery — the manual's own requirement, "show a delivery log so
    they can debug their end." Every attempt is recorded, successful or
    not, because the alternative — a customer whose endpoint silently
    stopped receiving events with no way to find out why — is exactly the
    kind of problem this feature exists to prevent, not create.
"""

from datetime import UTC, datetime
from typing import Literal

from beanie import Document
from pydantic import Field

# The event types this system can emit. A closed set rather than a free
# string: a customer choosing what to subscribe to needs to see the real
# list, and a typo'd event name that matches nothing should fail at
# registration time, not be discovered as "my webhook never fires."
EVENT_TYPES = frozenset({
    "appointment.booked",
    "appointment.cancelled",
    "appointment.rescheduled",
    "call.ended",
    # Task 3.10 — the caller has usually long hung up by the time a person
    # actually decides on a big action, so a webhook (not a live-call
    # announcement) is the realistic way a customer's own system finds out
    # and follows up — a callback, an SMS, whatever they already use.
    "approval.granted",
    "approval.denied",
})


class WebhookSubscription(Document):
    user_id: str  # whose platform account this belongs to (the bot's owner)
    event: str  # one of EVENT_TYPES
    url: str
    secret_encrypted: str
    enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "webhook_subscriptions"


class WebhookDelivery(Document):
    """One attempt to deliver one event to one subscription.

    Kept even for a subscription that has since been deleted (the
    subscription_id just stops resolving to anything) — a customer
    debugging "why did I stop getting these" needs the history from
    before they deleted it, not just what remains.
    """

    subscription_id: str
    event: str
    payload: dict = Field(default_factory=dict)
    attempt: int
    ok: bool
    status_code: int | None = None
    error: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "webhook_deliveries"


class WebhookOutboxItem(Document):
    """One event, queued for one subscription, waiting to be sent.

    Why a queue and not just an HTTP call made where the event happens: an
    event usually happens inside a call's own OS process (task 2.4), and
    that process can exit within seconds of the caller hanging up — the
    manual's "retry with increasing delays" can mean minutes of waiting,
    which cannot safely live inside a process that short-lived. Queuing a
    durable row here and letting the long-lived API process's own
    background loop (webhook_delivery_loop, in services/webhooks.py) work
    through it means a retry schedule survives the call that triggered it
    ending, being killed, or crashing outright.
    """

    subscription_id: str
    event: str
    payload: dict = Field(default_factory=dict)
    status: Literal["pending", "delivered", "failed"] = "pending"
    attempt: int = 0
    next_attempt_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "webhook_outbox"
