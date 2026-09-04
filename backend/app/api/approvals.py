"""Task 3.10 — where a person actually decides.

Everything up to this point (tool_registry.py's approval gate) only ever
QUEUES a big action; nothing here runs automatically. This is the other
half: a person looks at what's pending and says yes or no, and only then
does the underlying HTTP call happen at all.

The manual's own reasoning is worth repeating here specifically, because
it's what every decision in this file follows from: no company will let
an AI approve a large refund unsupervised. So neither does this route —
approving something is an authenticated action taken by the bot's owner,
exactly like every other write in this codebase, not a webhook, not a
public link, not something a caller can trigger from the phone.
"""

from datetime import UTC, datetime

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import get_current_user
from app.models.approval import PendingApproval
from app.models.bot_tool import BotTool
from app.models.user import User
from app.services.tool_registry import call_http_tool

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _out(a: PendingApproval) -> dict:
    return {
        "id": str(a.id),
        "tool_name": a.tool_name,
        "bot_id": a.bot_id,
        "arguments": a.arguments,
        "amount": a.amount,
        "threshold": a.threshold,
        "status": a.status,
        "created_at": a.created_at.isoformat(),
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "decided_by": a.decided_by,
    }


async def _owned_approval(approval_id: str, user_id: str) -> PendingApproval:
    try:
        approval = await PendingApproval.get(PydanticObjectId(approval_id))
    except Exception:
        approval = None
    if approval is None or approval.user_id != user_id:
        raise HTTPException(status_code=404, detail="Approval not found")
    return approval


@router.get("/")
async def list_approvals(
    status: str | None = None, current_user: User = Depends(get_current_user)
):
    """Newest first — same reasoning as the webhook delivery log: whoever
    opens this is almost always chasing the thing that just happened."""
    query = [PendingApproval.user_id == str(current_user.id)]
    if status:
        query.append(PendingApproval.status == status)
    approvals = (
        await PendingApproval.find(*query)
        .sort(-PendingApproval.created_at)  # type: ignore[arg-type]
        .to_list()
    )
    return [_out(a) for a in approvals]


@router.post("/{approval_id}/approve")
async def approve(approval_id: str, current_user: User = Depends(get_current_user)):
    """The action happens NOW, for the first time — not when it was
    originally requested. Everything about it (the tool, the arguments)
    was frozen at that moment; approving just releases it to actually run.
    """
    approval = await _owned_approval(approval_id, str(current_user.id))
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail=f"Already {approval.status}")

    tool = await BotTool.get(PydanticObjectId(approval.tool_id)) if approval.tool_id else None
    if tool is None:
        # The tool itself was edited or deleted since this was queued.
        # Refusing to run something whose configuration no longer exists is
        # the safe direction to fail in — a caller is told "denied", not
        # left believing an unrunnable action might still happen.
        approval.status = "denied"
        approval.decided_at = datetime.now(UTC)
        approval.decided_by = f"{current_user.email} (auto — tool no longer exists)"
        await approval.save()
        await _notify(approval, granted=False)
        raise HTTPException(
            status_code=409,
            detail="The tool this was for no longer exists — automatically denied.",
        )

    result = await call_http_tool(tool, approval.arguments)

    approval.status = "approved"
    approval.decided_at = datetime.now(UTC)
    approval.decided_by = current_user.email
    approval.executed = True
    approval.result = result
    await approval.save()

    await _notify(approval, granted=True)
    return _out(approval) | {"execution_result": result}


@router.post("/{approval_id}/deny")
async def deny(approval_id: str, current_user: User = Depends(get_current_user)):
    """The action never runs at all — the whole point of checking before
    rather than after."""
    approval = await _owned_approval(approval_id, str(current_user.id))
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail=f"Already {approval.status}")

    approval.status = "denied"
    approval.decided_at = datetime.now(UTC)
    approval.decided_by = current_user.email
    await approval.save()

    await _notify(approval, granted=False)
    return _out(approval)


async def _notify(approval: PendingApproval, granted: bool) -> None:
    """Task 3.8's webhook system doing double duty as this task's
    notification channel — the caller has almost always long hung up by
    the time a person actually decides, so a live-call announcement isn't
    the realistic path here; a customer's own webhook receiver forwarding
    this into Slack, email, or an SMS on their end is.

    Wrapped, like every other webhook emit() in this codebase: a
    notification failing must never undo or hide a decision that already
    happened.
    """
    try:
        from app.services.webhooks import emit

        await emit(
            "approval.granted" if granted else "approval.denied",
            user_id=approval.user_id,
            payload={
                "approval_id": str(approval.id),
                "tool_name": approval.tool_name,
                "amount": approval.amount,
                "threshold": approval.threshold,
                "decided_by": approval.decided_by,
            },
        )
    except Exception as e:
        from loguru import logger

        logger.warning(f"[APPROVAL] Could not queue notification for {approval.id}: {e}")
