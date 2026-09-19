"""Task 5.1 — who owns what, and who may do what with it.

An Organisation owns bots and everything attached to them. A Membership
says one user belongs to one organisation with one role. Many-to-many:
one person can be in several organisations (an agency, a consultant) with
a different role in each. See Docs/specs/2026-09-19-organisations-teams-roles-design.md.
"""

from datetime import UTC, datetime
from typing import Literal

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, IndexModel

Role = Literal["owner", "admin", "member", "viewer"]
ROLE_RANK: dict[str, int] = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}


class Organisation(Document):
    name: str
    created_by: str  # user id
    # Made automatically for a user (at sign-up, migration or self-heal).
    # Records origin only — a personal organisation can be shared like any other.
    personal: bool = False
    # Kept equal to the number of owner memberships. Lets "always at least
    # one owner" be enforced with one atomic conditional update instead of a
    # transaction (Atlas M0 does not document transaction support). Every
    # failure path leaves it LOW, which can only refuse a change, never
    # allow zero owners. scripts/migrate_orgs.py recounts it.
    owner_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "organisations"
        indexes = [
            # One personal organisation per user, whatever races happen:
            # two tabs logging in, registration + self-heal, a migration rerun.
            IndexModel(
                [("created_by", ASCENDING)],
                unique=True,
                partialFilterExpression={"personal": True},
                name="one_personal_org_per_user",
            ),
        ]


class Membership(Document):
    org_id: str
    user_id: str
    role: Role
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "memberships"
        indexes = [
            IndexModel(
                [("org_id", ASCENDING), ("user_id", ASCENDING)],
                unique=True,
                name="one_membership_per_user_per_org",
            ),
            IndexModel([("user_id", ASCENDING)], name="my_organisations"),
        ]
