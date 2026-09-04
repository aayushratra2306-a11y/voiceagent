"""Task 3.4 — undoing earlier steps when a later one fails.

The manual calls this the step almost everyone skips, and is specific about
why it matters: calling three APIs in a row is easy, correctly undoing the
first two when the third fails is not, and with real third-party APIs
failure happens constantly. Without it a half-finished workflow leaves real
charges on a real customer's card.

How it decides what to undo, which is the part worth getting right:

  Only a tool that DECLARES how to undo itself is ever compensated.

That is self-limiting in exactly the way you want. A lookup declares no
undo, so a batch of two lookups where one fails rolls nothing back — there
is nothing to roll back. A booking declares a cancel URL, so a booking that
succeeded alongside a payment that failed gets cancelled. The configuration
carries the intent; this file does not guess it.

The unit of work is one turn's batch of tool calls, because that is what
the caller asked for in one sentence: "book the cab and text me the
receipt" is one request, and half of it succeeding is the case this exists
for.

On the manual's warning that some things genuinely cannot be undone — a
sent message, a charged card — nothing here pretends otherwise. A tool
with no declared undo is reported as done and NOT reversed, a failed undo
is logged at error level with everything needed to fix it by hand, and
either way the model is told to say exactly what stands and what does not.
Silently leaving a mess is the outcome this is written to avoid.
"""

from typing import Any

from loguru import logger


class SagaStep:
    """One completed, reversible step in a turn."""

    def __init__(self, name: str, tool, arguments: dict[str, Any], result: dict[str, Any]):
        self.name, self.tool = name, tool
        self.arguments, self.result = arguments, result


class TurnSaga:
    """The completed steps of one turn, and how to walk them back.

    Reset between turns: rolling back a booking made two turns ago because
    something unrelated failed now would be its own kind of disaster.
    """

    def __init__(self, announce=None):
        self._steps: list[SagaStep] = []
        self._failed: list[str] = []
        self._irreversible: list[str] = []
        # Task 3.10 — kept separate from _irreversible on purpose: those two
        # need different sentences. "Succeeded and cannot be undone, so it
        # stands" is true of an irreversible success; it is actively FALSE
        # of something still waiting on a person, which has not happened at
        # all yet.
        self._pending_approval: list[str] = []
        self._expected = 0
        self._seen = 0
        # Called with the sentence the caller should hear. Supplied by the
        # pipeline, which owns the only thing that can speak.
        self._announce = announce

    @property
    def has_failure(self) -> bool:
        return bool(self._failed)

    def begin(self, expected: int) -> None:
        """A new batch of tool calls. Clears the last one.

        Rolling back a booking made two turns ago because something
        unrelated failed now would be its own kind of disaster, so each
        turn starts empty.
        """
        self._steps.clear()
        self._failed.clear()
        self._irreversible.clear()
        self._pending_approval.clear()
        self._expected = expected
        self._seen = 0

    # Kept as an alias so a caller that only wants to clear state reads
    # naturally.
    reset = begin

    async def record(self, name: str, tool, arguments: dict[str, Any], result: dict[str, Any]) -> None:
        """Note what a tool did, and roll back if that completes a bad batch.

        Pipecat emits an event when a batch of calls STARTS but not when it
        finishes, so completion is counted here: once as many results have
        arrived as calls were announced, the batch is done and a mixed
        outcome can be acted on. Counting rather than waiting also means a
        tool that never reports simply leaves the saga idle instead of
        hanging the turn.
        """
        if not result.get("ok", True):
            self._failed.append(name)
        elif result.get("pending_approval"):
            # Task 3.10 — the underlying action has NOT run yet, no matter
            # what this tool's own undo configuration says. A tool that
            # happens to declare both approval and undo has nothing to
            # roll back — it never ran — but it is also not "an
            # irreversible success", so it gets its own bucket rather than
            # borrowing a sentence that would misdescribe it either way.
            self._pending_approval.append(name)
        elif tool is not None and getattr(tool, "undo", None) and tool.undo.url:
            self._steps.append(SagaStep(name, tool, arguments, result))
        else:
            # Succeeded, cannot be taken back. Named so the caller can be
            # told it stands rather than left to assume it was undone.
            self._irreversible.append(name)

        self._seen += 1
        if self._expected and self._seen >= self._expected:
            await self._finish_batch()

    async def _finish_batch(self) -> None:
        """The batch is complete. Roll back only if it needs it."""
        self._expected = 0          # never act on the same batch twice
        if not self.has_failure:
            return
        if not self._steps and not self._irreversible and not self._pending_approval:
            # Everything failed. There is nothing to undo, and the failed
            # results already tell the model what to say.
            return

        summary = await self.roll_back()
        if self._announce:
            await self._announce(self.describe(summary))

    async def roll_back(self) -> dict[str, Any]:
        """Walk the completed steps backwards, undoing each.

        Reverse order because later steps may depend on earlier ones — the
        hotel booked against the flight has to go before the flight does.

        Returns a summary the model can turn into a sentence. Never raises:
        this runs while a caller is on the line, and an exception here would
        replace a bad-news sentence with silence.
        """
        from app.services.tool_registry import call_http_tool

        undone, failed_undo = [], []

        for step in reversed(self._steps):
            logger.warning(f"[SAGA] Undoing {step.name} because a later step failed")
            try:
                # The undo call sees the original arguments, so a cancel URL
                # can be written as /bookings/{booking_id} using the same
                # placeholders the booking used.
                result = await call_http_tool(step.tool.as_undo_tool(), step.arguments)
            except Exception as e:
                result = {"ok": False, "error": type(e).__name__, "message": str(e)}

            if result.get("ok"):
                logger.warning(f"[SAGA] {step.name} undone")
                undone.append(step.name)
            else:
                # Error, not warning: a failed undo is the one case here that
                # genuinely needs a human, because something real happened
                # and could not be taken back.
                logger.error(
                    f"[SAGA] COULD NOT UNDO {step.name} — this needs fixing by hand. "
                    f"arguments={step.arguments} result={result}"
                )
                failed_undo.append(step.name)

        return {
            "undone": undone,
            "could_not_undo": failed_undo,
            "left_standing": list(self._irreversible),
            "pending_approval": list(self._pending_approval),
            "failed": list(self._failed),
        }

    def describe(self, summary: dict[str, Any]) -> str:
        """Turn a rollback summary into an instruction the model can speak."""
        parts = []
        if summary["failed"]:
            parts.append(f"These did not work: {', '.join(summary['failed'])}.")
        if summary["undone"]:
            parts.append(f"These were undone automatically: {', '.join(summary['undone'])}.")
        if summary["left_standing"]:
            parts.append(
                f"These succeeded and CANNOT be undone, so they still stand: "
                f"{', '.join(summary['left_standing'])}."
            )
        if summary["could_not_undo"]:
            parts.append(
                f"These succeeded and could not be undone automatically: "
                f"{', '.join(summary['could_not_undo'])}. Tell the caller a person "
                f"will follow up about them — do not promise they are cancelled."
            )
        if summary.get("pending_approval"):
            parts.append(
                f"These have NOT happened yet and are waiting on a person to approve: "
                f"{', '.join(summary['pending_approval'])}. Do not describe them as done "
                f"or as failed — the caller will hear back separately once decided."
            )
        return (
            "Part of what the caller asked for did not complete. "
            + " ".join(parts)
            + " Tell them exactly this, plainly and without apologising at length. "
            "Do not describe anything as done unless it is listed as standing."
        )


# Added to the system prompt only for bots that have a reversible tool.
SAGA_RULE = (
    "\n\nIf several actions are requested together and one fails, some of the "
    "others may be undone automatically and some may not. You will be told "
    "exactly which. Repeat that to the caller accurately — never say everything "
    "worked, and never say everything failed, when neither is true."
)
