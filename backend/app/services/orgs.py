"""Task 5.1 — organisation membership logic that must not race.

No multi-document transactions: Atlas M0 does not document support. Safety
comes from unique indexes and single-document atomic updates instead.
"""

from datetime import UTC, datetime

from beanie import PydanticObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.models.organisation import Membership, Organisation, Role
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


class LastOwnerError(Exception):
    """The change would leave an organisation with no owner."""


class AlreadyMemberError(Exception):
    pass


def _orgs():
    return Organisation.get_motor_collection()


def _members():
    return Membership.get_motor_collection()


async def _inc(org_id: str, by: int) -> None:
    await _orgs().update_one({"_id": PydanticObjectId(org_id)}, {"$inc": {"owner_count": by}})


async def _take_one_owner_slot(org_id: str) -> None:
    """Atomic: only succeeds while there are at least two owners."""
    taken = await _orgs().find_one_and_update(
        {"_id": PydanticObjectId(org_id), "owner_count": {"$gt": 1}},
        {"$inc": {"owner_count": -1}},
    )
    if taken is None:
        raise LastOwnerError


async def add_member(org_id: str, user_id: str, role: Role) -> Membership:
    m = Membership(org_id=org_id, user_id=user_id, role=role)
    try:
        await m.insert()
    except DuplicateKeyError as e:
        raise AlreadyMemberError from e
    if role == "owner":
        await _inc(org_id, +1)  # only after the insert succeeded
    return m


async def set_role(org_id: str, user_id: str, new_role: Role) -> Membership:
    current = await Membership.find_one(Membership.org_id == org_id, Membership.user_id == user_id)
    if current is None or current.role == new_role:
        return current
    if current.role == "owner":
        await _take_one_owner_slot(org_id)
        res = await _members().update_one(
            {"org_id": org_id, "user_id": user_id, "role": "owner"}, {"$set": {"role": new_role}}
        )
        if res.modified_count == 0:  # someone else demoted them first
            await _inc(org_id, +1)
    elif new_role == "owner":
        res = await _members().update_one(
            {"org_id": org_id, "user_id": user_id, "role": {"$ne": "owner"}}, {"$set": {"role": "owner"}}
        )
        if res.modified_count:
            await _inc(org_id, +1)
    else:
        await _members().update_one(
            {"org_id": org_id, "user_id": user_id, "role": {"$ne": "owner"}}, {"$set": {"role": new_role}}
        )
    return await Membership.find_one(Membership.org_id == org_id, Membership.user_id == user_id)


async def remove_member(org_id: str, user_id: str) -> None:
    current = await Membership.find_one(Membership.org_id == org_id, Membership.user_id == user_id)
    if current is None:
        return
    if current.role == "owner":
        await _take_one_owner_slot(org_id)
        res = await _members().delete_one({"org_id": org_id, "user_id": user_id, "role": "owner"})
        if res.deleted_count == 0:
            await _inc(org_id, +1)
    else:
        await _members().delete_one({"org_id": org_id, "user_id": user_id, "role": {"$ne": "owner"}})


async def recount_owner_counts() -> int:
    corrected = 0
    async for org in _orgs().find({}, {"owner_count": 1}):
        real = await _members().count_documents({"org_id": str(org["_id"]), "role": "owner"})
        if org.get("owner_count") != real:
            await _orgs().update_one({"_id": org["_id"]}, {"$set": {"owner_count": real}})
            corrected += 1
    return corrected
