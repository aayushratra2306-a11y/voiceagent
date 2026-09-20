"""Task 5.1 — the organisation check every tenant route declares.

Order matters and is tested (tests/test_org_context.py):
  401 bad token (get_current_user runs first, so the frontend's
      refresh-on-401 keeps working)
  400 no organisation named
  404 malformed id, or not a member (identical, so ids can't be probed)
  403 member, but the role is too low

Resources are fetched by id AND org_id in ONE query (fetch_org_bot, ...),
never fetched then compared — OWASP API1's defence-in-depth advice. This
replaces app/core/deps.py's per-user helpers.
"""

from dataclasses import dataclass
from functools import cache

from beanie import PydanticObjectId
from bson.errors import InvalidId
from fastapi import Depends, Header, HTTPException

from app.core.auth import get_current_user
from app.models.bot import Bot
from app.models.document import Document
from app.models.organisation import ROLE_RANK, Membership, Role
from app.models.user import User

_NOT_FOUND = HTTPException(status_code=404, detail="Organisation not found")


@dataclass(frozen=True)
class OrgContext:
    user: User
    org_id: str
    role: str


def _object_id(value: str) -> PydanticObjectId | None:
    try:
        return PydanticObjectId(value)
    except (InvalidId, TypeError, ValueError):
        return None


async def _resolve(user: User, org_id: str | None) -> OrgContext:
    if not org_id:
        raise HTTPException(status_code=400, detail="Choose an organisation")
    if _object_id(org_id) is None:
        raise _NOT_FOUND
    membership = await Membership.find_one(
        Membership.org_id == org_id, Membership.user_id == str(user.id)
    )
    if membership is None:
        raise _NOT_FOUND
    return OrgContext(user=user, org_id=org_id, role=membership.role)


async def get_org_context(
    x_org_id: str | None = Header(default=None),
    user: User = Depends(get_current_user),
) -> OrgContext:
    return await _resolve(user, x_org_id)


async def get_org_context_from_path(
    org_id: str,
    x_org_id: str | None = Header(default=None),
    user: User = Depends(get_current_user),
) -> OrgContext:
    # The path names the organisation being acted on. A header naming a
    # different one is refused, so an admin of A cannot act on /orgs/B by
    # sending A's header (spec C1).
    if x_org_id is not None and x_org_id != org_id:
        raise _NOT_FOUND
    return await _resolve(user, org_id)


@cache
def require_role(minimum: Role, *, from_path: bool = False):
    """Cached, so FastAPI sees ONE callable per (minimum, from_path) and
    resolves the membership once per request even if a route declares this
    and org_bot(...) together."""
    source = get_org_context_from_path if from_path else get_org_context

    async def dependency(ctx: OrgContext = Depends(source)) -> OrgContext:
        if ROLE_RANK[ctx.role] < ROLE_RANK[minimum]:
            raise HTTPException(status_code=403, detail=f"Your role ({ctx.role}) can't do this")
        return ctx

    dependency.__name__ = f"require_{minimum}{'_from_path' if from_path else ''}"
    return dependency


async def fetch_org_bot(bot_id: str, ctx: OrgContext) -> Bot:
    oid = _object_id(bot_id)
    bot = await Bot.find_one(Bot.id == oid, Bot.org_id == ctx.org_id) if oid else None
    if bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot


@cache
def org_bot(minimum: Role):
    async def dependency(bot_id: str, ctx: OrgContext = Depends(require_role(minimum))) -> Bot:
        return await fetch_org_bot(bot_id, ctx)

    dependency.__name__ = f"org_bot_{minimum}"
    return dependency


@cache
def org_document(minimum: Role):
    async def dependency(doc_id: str, ctx: OrgContext = Depends(require_role(minimum))) -> Document:
        oid = _object_id(doc_id)
        doc = await Document.find_one(Document.id == oid, Document.org_id == ctx.org_id) if oid else None
        if doc is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return doc

    dependency.__name__ = f"org_document_{minimum}"
    return dependency
