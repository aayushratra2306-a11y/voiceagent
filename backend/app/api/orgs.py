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
from app.models.approval import PendingApproval
from app.models.bot import Bot
from app.models.organisation import ROLE_RANK, Membership, Organisation, Role
from app.models.user import User
from app.models.webhook import WebhookSubscription
from app.services import orgs as svc

router = APIRouter(prefix="/orgs", tags=["organisations"])


class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class MemberIn(BaseModel):
    email: EmailStr  # same normalisation as registration, so lookups match
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
        {"user_id": m.user_id, "email": users[m.user_id].email, "role": m.role, "joined": m.created_at.isoformat()}
        for m in members if m.user_id in users
    ]


def _guard_owner_rules(ctx: OrgContext, *roles_involved: str) -> None:
    """An admin cannot add, remove, promote to or demote an owner."""
    if "owner" in roles_involved and ctx.role != "owner":
        raise HTTPException(status_code=403, detail="Only an owner can change another owner")


@router.post("/{org_id}/members", status_code=201)
@limiter.limit("10/minute")
async def add_member(
    request: Request, body: MemberIn, ctx: OrgContext = Depends(require_role("admin", from_path=True))
):
    _guard_owner_rules(ctx, body.role)
    target = await User.find_one(User.email == body.email)
    if target is None:
        # Tells an admin whether an address has an account. Accepted for 5.1
        # (admins only, rate-limited); 5.2's invitations make it uniform.
        raise HTTPException(status_code=404, detail="No Voix account uses that email")
    try:
        await svc.add_member(ctx.org_id, str(target.id), body.role)
    except svc.AlreadyMemberError as e:
        raise HTTPException(status_code=409, detail="Already a member") from e
    return {"user_id": str(target.id), "email": target.email, "role": body.role}


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
    return Response(status_code=204)
