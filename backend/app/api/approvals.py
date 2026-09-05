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
    status: str | None = None,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
):
    """Newest first — same reasoning as the webhook delivery log: whoever
    opens this is almost always chasing the thing that just happened.

    Bounded, like the delivery log: an account that has been running a
    while accumulates decided approvals indefinitely, and nobody opening
    this page wants every one of them ever.
    """
    query = [PendingApproval.user_id == str(current_user.id)]
    if status:
        query.append(PendingApproval.status == status)
    approvals = (
        await PendingApproval.find(*query)
        .sort(-PendingApproval.created_at)  # type: ignore[arg-type]
        .limit(max(1, min(limit, 500)))
        .to_list()
    )
    return [_out(a) for a in approvals]


async def _claim_for_decision(approval: PendingApproval, new_status: str, decided_by: str) -> bool:
    """Move this approval out of "pending" atomically, or report that it
    already left.

    Reading `status == "pending"` and then acting on it is two operations,
    and two requests can both pass the read before either writes — a
    double-click, an impatient second click, two people on the same
    account, a retried request. For a deny that would be harmless. For an
    approve it means `call_http_tool` runs twice: the refund is issued
    twice, the payout sent twice. That is precisely the outcome task 3.10
    exists to make impossible, so guarding it with a plain if-statement
    was not enough.

    One conditional update decides it instead. Whichever request MongoDB
    applies first flips the status; the other's filter no longer matches,
    it gets nothing back, and it is refused with a 409.
    """
    updated = await PendingApproval.get_motor_collection().update_one(
        {"_id": approval.id, "status": "pending"},
        {"$set": {
            "status": new_status,
            "decided_at": datetime.now(UTC),
            "decided_by": decided_by,
        }},
    )
    return updated.modified_count == 1


@router.post("/{approval_id}/approve")
async def approve(approval_id: str, current_user: User = Depends(get_current_user)):
    """The action happens NOW, for the first time — not when it was
    originally requested. Everything about it (the tool, the arguments)
    was frozen at that moment; approving just releases it to actually run.
    """
    approval = await _owned_approval(approval_id, str(current_user.id))
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail=f"Already {approval.status}")

    # Claimed BEFORE the action runs, not after — see _claim_for_decision.
    # "approving" rather than "approved": the action has not happened yet,
    # and if this process dies during it, a record that already says
    # "approved" would be a lie about something nobody can confirm.
    if not await _claim_for_decision(approval, "approving", current_user.email):
        raise HTTPException(status_code=409, detail="This was already decided.")
    approval.status = "approving"
    approval.decided_by = current_user.email

    # A malformed or non-ObjectId tool_id must land in the same "deny"
    # branch as a genuinely deleted tool, not raise into a 500 — the
    # underlying config is equally unrunnable either way, and both are
    # "the tool this pointed at no longer resolves."
    try:
        tool = await BotTool.get(PydanticObjectId(approval.tool_id)) if approval.tool_id else None
    except Exception:
        tool = None
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

    # Same atomic claim as approve. Denying twice would be harmless in
    # itself, but going through the same door means a deny racing an
    # approve cannot both win — which is not harmless at all.
    if not await _claim_for_decision(approval, "denied", current_user.email):
        raise HTTPException(status_code=409, detail="This was already decided.")
    approval.status = "denied"
    approval.decided_at = datetime.now(UTC)
    approval.decided_by = current_user.email

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
