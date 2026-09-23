"""Task 5.2 — an admin invites someone by email; the invitee opens a link
and joins. This is the stored record of that offer.

Carries org_id, so (per app/models/registry.py's ORG_EXEMPT_MODELS rule) it
is tenant data and is NOT exempt from organisation scoping.
"""

from datetime import UTC, datetime

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.models.organisation import Role


class Invitation(Document):
    org_id: str
    email: str  # normalised, lowercase — see app/services/invitations.py
    role: Role
    # sha256 hex of the raw token. The raw token itself is never stored (see
    # app/services/invitations.py create_invitation) so a database leak
    # yields no usable invitation links.
    token_hash: str
    invited_by: str  # user id
    status: str = "pending"  # "pending" | "accepted" | "revoked"
    expires_at: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    accepted_at: datetime | None = None
    accepted_by: str | None = None  # user id

    class Settings:
        name = "invitations"
        indexes = [
            IndexModel([("token_hash", ASCENDING)], unique=True, name="unique_token_hash"),
            IndexModel([("org_id", ASCENDING), ("status", ASCENDING)], name="org_pending_invitations"),
        ]
