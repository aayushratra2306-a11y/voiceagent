"""The one list of stored models (task 5.1).

There used to be four hand-maintained copies of this list (main.py,
call_worker.py, tests/conftest.py, scripts/loadtest_accounts.py). One list
means the model-coverage test (tests/test_org_models.py) checks what the
app actually registers.
"""

from app.models.appointment import Appointment
from app.models.approval import PendingApproval
from app.models.bot import Bot
from app.models.bot_tool import BotTool
from app.models.conversation import ConversationTurn
from app.models.document import Document
from app.models.order import Order
from app.models.organisation import Membership, Organisation
from app.models.payment import PaymentSession
from app.models.revoked_token import RevokedRefreshToken
from app.models.user import User
from app.models.webhook import WebhookDelivery, WebhookOutboxItem, WebhookSubscription

ALL_MODELS = [
    User, Bot, Document, Order, Appointment, ConversationTurn, RevokedRefreshToken,
    BotTool, PaymentSession, WebhookSubscription, WebhookDelivery, WebhookOutboxItem,
    PendingApproval, Organisation, Membership,
]

# Models deliberately without org_id, each with the reason.
ORG_EXEMPT_MODELS: dict[type, str] = {
    User: "a login, which can belong to several organisations through Membership",
    RevokedRefreshToken: "a per-login security record, not tenant data",
    Organisation: "is the tenant itself",
    Membership: "links a user to an organisation; carries org_id as its key",
    Order: "Phase 1 demo data shared by every bot; flagged in the spec, not tenant data",
    WebhookDelivery: "reached only through its subscription, which is org-scoped",
    WebhookOutboxItem: "reached only through its subscription, which is org-scoped",
}
