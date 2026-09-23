"""Task 5.1 — organisations and their members.

/orgs/{org_id}/... takes the organisation from the PATH
(require_role(..., from_path=True)); see app/core/org.py for why a
different X-Org-Id header is refused.
"""

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field

from app.core.auth import get_current_user
from app.core.org import OrgContext, require_role
from app.core.rate_limit import limiter
from app.core.times import utc_isoformat
from app.models.approval import PendingApproval
from app.models.bot import Bot
from app.models.organisation import ROLE_RANK, Membership, Organisation, Role
from app.models.user import User
from app.models.webhook import WebhookSubscription
from app.services import invitations as inv
from app.services import orgs as svc

router = APIRouter(prefix="/orgs", tags=["organisations"])


class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class MemberIn(BaseModel):
    # EmailStr validates the shape. It does NOT normalise the way this
    # feature compares addresses: it lowercases only the domain, while
    # invitations._normalise_email lowercases the whole address. Nothing
    # looks the address up here any more (task 5.2 removed that, with the
    # 404 that leaked whether it was registered) — the comparison happens
    # at accept time.
    email: EmailStr
    role: Role


class RoleIn(BaseModel):
    role: Role


@router.get("")
async def my_orgs(user: User = Depends(get_current_user)):
    await svc.ensure_personal_org(user)  # the self-heal (spec I5)
    memberships = await Membership.find(Membership.user_id == str(user.id)).to_list()
    by_id = {str(o.id): o for o in await Organisation.find(
        {"_id": {"$in": [PydanticObjectId(m.org_id) for m in memberships]}}
    ).to_list()}
    return [
        {"id": m.org_id, "name": by_id[m.org_id].name, "personal": by_id[m.org_id].personal, "role": m.role}
        for m in memberships if m.org_id in by_id
    ]


@router.post("", status_code=201)
@limiter.limit("10/minute")
async def create_org(request: Request, body: OrgIn, user: User = Depends(get_current_user)):
    org = Organisation(name=body.name, created_by=str(user.id), owner_count=0)
    await org.insert()
    await svc.add_member(str(org.id), str(user.id), "owner")
    return {"id": str(org.id), "name": org.name, "personal": False, "role": "owner"}


@router.patch("/{org_id}")
async def rename_org(body: OrgIn, ctx: OrgContext = Depends(require_role("admin", from_path=True))):
    await Organisation.get_motor_collection().update_one(
        {"_id": PydanticObjectId(ctx.org_id)}, {"$set": {"name": body.name}}
    )
    return {"id": ctx.org_id, "name": body.name}


@router.delete("/{org_id}", status_code=204)
async def delete_org(ctx: OrgContext = Depends(require_role("owner", from_path=True))):
    if await Membership.find(Membership.user_id == str(ctx.user.id)).count() <= 1:
        raise HTTPException(status_code=409, detail="You can't delete your only organisation")
    busy = (
        await Bot.find_one(Bot.org_id == ctx.org_id)
        or await WebhookSubscription.find_one(WebhookSubscription.org_id == ctx.org_id)
        or await PendingApproval.find_one(PendingApproval.org_id == ctx.org_id, PendingApproval.status == "pending")
    )
    if busy:
        raise HTTPException(
            status_code=409,
            detail="Only an empty organisation can be deleted: remove its bots, webhooks and pending approvals first",
        )
    # Pending invitations die with the organisation. Without this a link
    # sent yesterday still resolves today and, before the matching guard in
    # invitations.accept, wrote a membership pointing at an org that no
    # longer exists. Done BEFORE the deletes so an interruption leaves dead
    # invitations to a live org — recoverable — rather than live
    # invitations to a deleted one.
    await inv.revoke_all_for_org(ctx.org_id)
    await Membership.find(Membership.org_id == ctx.org_id).delete()
    await Organisation.get_motor_collection().delete_one({"_id": PydanticObjectId(ctx.org_id)})
    return Response(status_code=204)


@router.get("/{org_id}/members")
async def list_members(ctx: OrgContext = Depends(require_role("viewer", from_path=True))):
    members = await Membership.find(Membership.org_id == ctx.org_id).to_list()
    users = {str(u.id): u for u in await User.find(
        {"_id": {"$in": [PydanticObjectId(m.user_id) for m in members]}}
    ).to_list()}
    return [
        {"user_id": m.user_id, "email": users[m.user_id].email, "role": m.role, "joined": utc_isoformat(m.created_at)}
        for m in members if m.user_id in users
    ]


def _guard_owner_rules(ctx: OrgContext, *roles_involved: str) -> None:
    """An admin cannot add, remove, promote to or demote an owner."""
    if "owner" in roles_involved and ctx.role != "owner":
        raise HTTPException(status_code=403, detail="Only an owner can change another owner")


@router.post("/{org_id}/members", status_code=202)
@limiter.limit("10/minute")
async def add_member(
    request: Request, body: MemberIn, ctx: OrgContext = Depends(require_role("admin", from_path=True))
):
    """Invites `body.email` to join, rather than adding them directly.

    Deliberately does NOT look the address up in User first (that was the
    5.1 defect: a 404 for an unknown address vs a 201 for a known one told
    an admin whether an email had a Voix account at all). Every outcome —
    unknown address, known address, already a member of this org, already
    invited — returns this same 202 with this same body shape. Whether the
    address is already a member is left for accept() to discover (see
    app/services/invitations.py); surfacing it here would reopen the leak.
    """
    _guard_owner_rules(ctx, body.role)
    invite, raw_token = await inv.create_invitation(ctx.org_id, body.email, body.role, str(ctx.user.id))
    invite_path = f"/invite/{raw_token}"
    await inv.deliver_invitation(invite, invite_path)
    return {"status": "invitation sent", "invite_path": invite_path, "expires_at": utc_isoformat(invite.expires_at)}


@router.patch("/{org_id}/members/{user_id}")
async def change_role(user_id: str, body: RoleIn, ctx: OrgContext = Depends(require_role("admin", from_path=True))):
    current = await Membership.find_one(Membership.org_id == ctx.org_id, Membership.user_id == user_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Not a member")
    _guard_owner_rules(ctx, current.role, body.role)
    try:
        await svc.set_role(ctx.org_id, user_id, body.role)
    except svc.LastOwnerError as e:
        raise HTTPException(status_code=409, detail="An organisation needs at least one owner") from e
    # Losing the right to invite takes the outstanding invitations with it.
    # Only a demotion below admin: a promotion leaves them alone. Done after
    # the role change, so an interruption leaves the weaker role with live
    # invitations (visible, revocable by any admin) rather than the stronger
    # role with none.
    if ROLE_RANK[body.role] < ROLE_RANK["admin"]:
        await inv.revoke_all_by_inviter(ctx.org_id, user_id)
    return {"user_id": user_id, "role": body.role}


@router.delete("/{org_id}/members/{user_id}", status_code=204)
async def remove_member(user_id: str, ctx: OrgContext = Depends(require_role("viewer", from_path=True))):
    leaving = user_id == str(ctx.user.id)
    if not leaving and ROLE_RANK[ctx.role] < ROLE_RANK["admin"]:
        raise HTTPException(status_code=403, detail=f"Your role ({ctx.role}) can't do this")
    current = await Membership.find_one(Membership.org_id == ctx.org_id, Membership.user_id == user_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Not a member")
    if not leaving:
        _guard_owner_rules(ctx, current.role)
    try:
        await svc.remove_member(ctx.org_id, user_id)
    except svc.LastOwnerError as e:
        raise HTTPException(status_code=409, detail="An organisation needs at least one owner") from e
    # Same rule as the demotion above: someone who is no longer in the
    # organisation must not keep handing out roles in it for the rest of the
    # 7 days, to whoever holds a link they sent before they left.
    await inv.revoke_all_by_inviter(ctx.org_id, user_id)
    return Response(status_code=204)


@router.get("/{org_id}/invitations")
async def list_invitations(ctx: OrgContext = Depends(require_role("admin", from_path=True))):
    pending = await inv.list_pending(ctx.org_id)
    return [
        {
            "id": str(invite.id),
            "email": invite.email,
            "role": invite.role,
            "invited_at": utc_isoformat(invite.created_at),
            "expires_at": utc_isoformat(invite.expires_at),
        }
        for invite in pending
    ]


@router.delete("/{org_id}/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(
    invitation_id: str, ctx: OrgContext = Depends(require_role("admin", from_path=True))
):
    # Both calls match _id AND org_id in one query (see their docstrings), so
    # another organisation's invitation id can never be reached through this
    # org's path — the same rule fetch_org_bot etc. follow.
    pending = await inv.get_pending(ctx.org_id, invitation_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    # Cancelling an owner invitation is an owner-touching action, like adding,
    # removing, promoting and demoting one. An admin ranks below an owner and
    # must not undo an owner's succession plan on their own.
    _guard_owner_rules(ctx, pending.role)
    if not await inv.revoke(ctx.org_id, invitation_id):
        raise HTTPException(status_code=404, detail="Invitation not found")
    return Response(status_code=204)
