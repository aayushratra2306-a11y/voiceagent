"""Task 5.2 — an admin invites someone by email; the invitee opens a link
and joins.

No transactions: Atlas M0 has none usable. `accept` adds the membership
FIRST and only then marks the invitation accepted (see its docstring for
what happens if it is interrupted in between).
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from beanie import PydanticObjectId
from loguru import logger

from app.models.invitation import Invitation
from app.models.organisation import Organisation, Role
from app.models.user import User
from app.services import orgs as org_service

INVITATION_EXPIRY_DAYS = 7


class InvitationError(Exception):
    pass


class NotFoundError(InvitationError):
    """Unknown, expired, revoked, or already-used token.

    One exception for all of those, deliberately — see find_valid.
    """


class EmailMismatchError(InvitationError):
    pass


class AlreadyMemberError(InvitationError):
    pass


def _normalise_email(email: str) -> str:
    # Lowercases the WHOLE address (local part included), so an invitation
    # matches an address however it happens to be typed — that is what
    # people expect of email, and this comparison behaviour is deliberately
    # being kept.
    #
    # This is NOT the same rule the users index applies, despite the
    # similar intent. User.email is Annotated[EmailStr, Indexed(unique=True)]
    # with no collation: pydantic's EmailStr lowercases only the domain
    # part, so uniqueness there is case-SENSITIVE on the local part.
    # "Admin@x.com" and "admin@x.com" can both exist as separate accounts.
    # Because this function lowercases the local part too, one invitation
    # can therefore be satisfied by either of two such case-variant
    # accounts. That is a pre-existing gap in the users index (a possible
    # auth issue), not something this function can or should fix.
    return email.strip().lower()


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def create_invitation(org_id: str, email: str, role: Role, invited_by: str) -> tuple[Invitation, str]:
    """Creates the stored invitation and returns (invitation, raw_token).

    The raw token exists only in this return value and whatever URL the
    caller builds from it — never stored. Only its hash is persisted, so a
    database leak yields no usable invitation links.
    """
    raw = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    invite = Invitation(
        org_id=org_id,
        email=_normalise_email(email),
        role=role,
        token_hash=hash_token(raw),
        invited_by=invited_by,
        expires_at=now + timedelta(days=INVITATION_EXPIRY_DAYS),
        created_at=now,
    )
    await invite.insert()
    return invite, raw


async def find_valid(raw_token: str) -> Invitation:
    """Returns the invitation for this token if it is usable right now.

    Mirrors verify_refresh_token (app/core/auth.py): unknown token, wrong
    status and expiry are three different checks but ALL raise the same
    NotFoundError, so a caller probing tokens learns nothing about which
    check failed. All three conditions are applied in one query rather than
    fetched-then-compared.
    """
    invite = await Invitation.find_one(
        Invitation.token_hash == hash_token(raw_token),
        Invitation.status == "pending",
        Invitation.expires_at > datetime.now(UTC),
    )
    if invite is None:
        raise NotFoundError
    return invite


async def accept(raw_token: str, user: User) -> Invitation:
    """Finds the invitation, checks the email, joins the org, then marks
    the invitation accepted — in that order.

    No transaction is available (Atlas M0), so those last two writes are
    not atomic. If the process is interrupted after add_member succeeds but
    before the invitation is saved as accepted, the invitation is left
    "pending" for someone who is already a member. A retry of accept() then
    finds that invitation valid again, matches the email again, and calls
    add_member again — which raises orgs.AlreadyMemberError because the
    membership already exists. That is deliberately re-raised as
    AlreadyMemberError here rather than swallowed: the retry does not
    silently succeed a second time or duplicate anything, it just tells the
    caller they're already in. The invitation itself is left pending
    forever in that narrow window (a stuck-but-harmless row) unless an
    admin revokes it.
    """
    invite = await find_valid(raw_token)

    # An invitation must not outlive the organisation it points at. delete_org
    # now revokes pending invitations, but that is not enough on its own: an
    # org can be deleted between find_valid and add_member, and invitations
    # created before that fix are still out there. Without this check,
    # add_member writes a Membership for an organisation that no longer
    # exists and the invitee is told they joined something that is gone.
    # Raised as NotFoundError so a dead invitation is indistinguishable from
    # any other unusable one.
    if await Organisation.get(PydanticObjectId(invite.org_id)) is None:
        raise NotFoundError

    if _normalise_email(user.email) != invite.email:
        raise EmailMismatchError

    try:
        await org_service.add_member(invite.org_id, str(user.id), invite.role)
    except org_service.AlreadyMemberError as e:
        raise AlreadyMemberError from e

    invite.status = "accepted"
    invite.accepted_at = datetime.now(UTC)
    invite.accepted_by = str(user.id)
    await invite.save()
    return invite


async def revoke(org_id: str, invitation_id: str) -> bool:
    """Marks a pending invitation revoked. Org-scoped in one query — id and
    org_id are matched together, never fetched then compared, so another
    organisation's id can never revoke this invitation.

    Returns whether this call did it: False for a wrong org, an unknown id,
    or an invitation that is already accepted/revoked.
    """
    try:
        object_id = PydanticObjectId(invitation_id)
    except Exception:
        return False
    result = await Invitation.get_motor_collection().update_one(
        {"_id": object_id, "org_id": org_id, "status": "pending"},
        {"$set": {"status": "revoked"}},
    )
    return result.modified_count > 0


async def get_pending(org_id: str, invitation_id: str) -> Invitation | None:
    """One pending invitation, matched by id AND org_id in a single query —
    never fetched then compared, so another organisation's id cannot reach
    this one.

    Exists so a caller can inspect an invitation's role before acting on
    it: revoking an *owner* invitation is an owner-touching action and has
    to be guarded like every other one, which needs the role first.
    """
    try:
        object_id = PydanticObjectId(invitation_id)
    except Exception:
        return None
    return await Invitation.find_one(
        Invitation.id == object_id,
        Invitation.org_id == org_id,
        Invitation.status == "pending",
    )


async def revoke_all_for_org(org_id: str) -> int:
    """Revokes every pending invitation for an organisation, and returns how
    many. Called when the organisation is deleted: a link that still works
    after its destination is gone is a loose end at best, and at worst
    writes a membership pointing at nothing.
    """
    result = await Invitation.get_motor_collection().update_many(
        {"org_id": org_id, "status": "pending"},
        {"$set": {"status": "revoked"}},
    )
    return result.modified_count


async def revoke_all_by_inviter(org_id: str, user_id: str, roles: list[str] | None = None) -> int:
    """Revokes every pending invitation a given person sent in a given
    organisation -- or, with `roles`, only those offering one of those
    roles -- and returns how many.

    An invitation is an exercise of authority: it hands out a role the
    sender was entitled to hand out. When they lose that entitlement --
    removed from the organisation, or demoted -- their outstanding
    invitations must not keep granting it for the rest of the 7 days, to
    whoever happens to hold the link. delete_org already had this
    reasoning for the organisation itself; this is the same rule for the
    person. `roles` exists for a partial loss: an owner demoted to admin
    can still invite below owner, so only their owner invitations go.
    """
    query = {"org_id": org_id, "invited_by": user_id, "status": "pending"}
    if roles is not None:
        query["role"] = {"$in": roles}
    result = await Invitation.get_motor_collection().update_many(
        query,
        {"$set": {"status": "revoked"}},
    )
    return result.modified_count


async def list_pending(org_id: str) -> list[Invitation]:
    """Only invitations that could still actually be used.

    Filters expiry as well as status, matching find_valid. Without the
    expiry check an invitation nobody can accept any more sat in the
    admin's "Pending invitations" list for ever, offering a Revoke button
    and showing a date in the past.
    """
    return await Invitation.find(
        Invitation.org_id == org_id,
        Invitation.status == "pending",
        Invitation.expires_at > datetime.now(UTC),
    ).sort(+Invitation.created_at).to_list()


async def deliver_invitation(invitation: Invitation, url: str) -> None:
    """Seam for task 7.10 (real email delivery).

    In 5.2 there is no email sending at all: delivery is by hand — the
    admin is shown the link and copies it to whoever they're inviting.
    This function only logs that an invitation exists to be delivered.

    `url` is accepted (and will be used to actually send the email once
    7.10 lands) but is deliberately NEVER logged, and neither is anything
    built from it. It carries the raw invitation token — the only thing
    gating membership of an organisation — and logs are read by more
    people, kept for longer, and shipped to more places than the database
    ever is. Logging it would be strictly worse than storing the token
    itself, which create_invitation already goes out of its way not to do.
    Do not add it back.
    """
    msg = (
        f"[INVITATIONS] invitation {invitation.id} for {invitation.email} "
        f"to org {invitation.org_id}: ready for hand delivery"
    )
    logger.info(msg)
