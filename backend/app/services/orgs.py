"""Task 5.1 — organisation membership logic that must not race.

No multi-document transactions: Atlas M0 does not document support. Safety
comes from unique indexes and single-document atomic updates instead.
"""

from datetime import UTC, datetime

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.models.organisation import Membership, Organisation
from app.models.user import User


def _personal_name(user: User) -> str:
    return f"{str(user.email).split('@')[0]}'s workspace"


async def ensure_personal_org(user: User) -> Organisation | None:
    """Make sure the user belongs to at least one organisation.

    Called by registration, by GET /orgs (the self-heal) and by the
    migration. Safe to call concurrently: the partial unique index
    one_personal_org_per_user turns a duplicate insert into a read, and the
    membership unique index does the same for the owner row.
    """
    uid = str(user.id)
    if await Membership.find_one(Membership.user_id == uid):
        return None

    orgs = Organisation.get_motor_collection()
    existing = await orgs.find_one({"created_by": uid, "personal": True})
    if existing is not None:
        # Reuse it unless someone else is in it — then this user was removed
        # from their own personal org, and it now belongs to others.
        others = await Membership.find_one(
            Membership.org_id == str(existing["_id"]), Membership.user_id != uid
        )
        if others is not None:
            await orgs.update_one({"_id": existing["_id"]}, {"$set": {"personal": False}})

    now = datetime.now(UTC)
    try:
        raw = await orgs.find_one_and_update(
            {"created_by": uid, "personal": True},
            {"$setOnInsert": {
                "name": _personal_name(user), "created_by": uid, "personal": True,
                "owner_count": 1, "created_at": now,
            }},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        raw = await orgs.find_one({"created_by": uid, "personal": True})

    org_id = str(raw["_id"])
    try:
        await Membership.get_motor_collection().update_one(
            {"org_id": org_id, "user_id": uid},
            {"$setOnInsert": {"org_id": org_id, "user_id": uid, "role": "owner", "created_at": now}},
            upsert=True,
        )
    except DuplicateKeyError:
        pass  # a concurrent call inserted the same row
    return await Organisation.get(raw["_id"])
