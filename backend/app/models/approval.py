"""Task 3.10 — big actions wait for a person.

The manual's own reasoning: no company will let an AI approve a large
refund unsupervised, and having this is what makes them comfortable
letting it handle everything BELOW the threshold — which is where the
actual call volume is. This is the record of one action that crossed a
customer-configured line and is waiting on a person before it happens at
all.

Notably: the underlying HTTP call has NOT happened yet when this is
created. Task 3.7's payment sessions and 3.4's saga both undo something
that already occurred; this instead holds the action itself until someone
says yes, which is a stronger guarantee than any undo could offer — there
is nothing to roll back if it never ran.
"""

from datetime import UTC, datetime
from typing import Any, Literal

from beanie import Document
from pydantic import Field


class PendingApproval(Document):
    tool_id: str
    bot_id: str
    user_id: str  # the bot's owner — who is allowed to decide this
    # The live call this came from, if any. Almost always long gone by the
    # time a person actually approves something (see the module docstring
    # below on notification) — kept anyway because a call that happens to
    # still be open when the decision lands should still hear about it.
    pc_id: str = ""

    tool_name: str  # for display — the tool row itself may be edited/deleted later
    arguments: dict[str, Any] = Field(default_factory=dict)
    amount: float
    threshold: float

    status: Literal["pending", "approved", "denied"] = "pending"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None
    # Free text rather than a user id: this codebase has no separate staff/
    # role concept, and "who approved this" is worth recording even as
    # plain text a solo operator types in.
    decided_by: str = ""
    # Set once the underlying action has actually been executed, so a
    # crash or a double-click on "approve" can't run it twice.
    executed: bool = False
    result: dict[str, Any] = Field(default_factory=dict)

    class Settings:
        name = "pending_approvals"
