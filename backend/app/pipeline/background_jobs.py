"""Task 3.3 — tools that take too long to wait for.

Some real APIs take five or ten seconds. On a phone call that is unbearable
silence and people hang up, so a tool marked long-running starts in the
background and returns immediately with an acknowledgement the model can
speak. When it finishes, the result is injected back into the conversation
and the model tells the caller — interrupting whatever is being discussed,
which is exactly what a person would do.

The injection uses `LLMMessagesAppendFrame(run_llm=True)`: it appends a
message and triggers a reply, so the model phrases the outcome itself
rather than the pipeline speaking a canned sentence at the caller.

The hang-up case, which the manual singles out, is decided deliberately
here rather than left to whatever happens:

  A job in flight is NOT cancelled when the caller hangs up.

Cancelling an HTTP request that may already have booked something is how
you end up with a booking nobody knows about — the remote system's state
becomes genuinely unknowable. So the request is allowed to finish and the
outcome is logged at warning level, loudly, with the arguments. Nobody
heard it, so the log is the only record that it happened. Where money is
involved the manual's answer is to escalate to a human, and that log line
is what a human would be escalated with.
"""

import asyncio
from typing import Any

from loguru import logger
from pipecat.frames.frames import LLMMessagesAppendFrame

# Long enough for the slow APIs this exists for, short enough that a job
# cannot outlive a call by minutes. Ten seconds was the manual's own example
# of an unbearable wait; this is the ceiling on the work itself.
JOB_TIMEOUT_SECONDS = 60.0


class BackgroundJobs:
    """The long-running jobs belonging to one call.

    One instance per call, holding the pipeline task so a finished job can
    speak into the conversation it belongs to.
    """

    def __init__(self, task=None):
        self._task = task
        self._running: set[asyncio.Task] = set()
        self._call_ended = False

    def attach(self, task) -> None:
        """Give it the pipeline task once that exists.

        The tools are built before the PipelineTask, so the reference
        arrives afterwards rather than in the constructor.
        """
        self._task = task

    @property
    def in_flight(self) -> int:
        return len(self._running)

    def start(self, name: str, coro, arguments: dict[str, Any] | None = None) -> None:
        """Run `coro` in the background and announce its result when done."""
        job = asyncio.create_task(self._run(name, coro, arguments or {}))
        self._running.add(job)
        # discard, not remove: the set is also cleared by shutdown().
        job.add_done_callback(self._running.discard)
        logger.info(f"[JOB] {name} started in the background ({self.in_flight} running)")

    async def _run(self, name: str, coro, arguments: dict[str, Any]) -> None:
        try:
            result = await asyncio.wait_for(coro, timeout=JOB_TIMEOUT_SECONDS)
        except TimeoutError:
            result = {
                "ok": False,
                "error": "timeout",
                "message": f"{name} did not finish in time.",
            }
        except asyncio.CancelledError:
            # Only reachable if something cancels us explicitly; shutdown()
            # deliberately does not. Nothing to announce either way.
            logger.warning(f"[JOB] {name} was cancelled mid-flight")
            raise
        except Exception as e:
            logger.warning(f"[JOB] {name} raised {type(e).__name__}: {e}")
            result = {"ok": False, "error": "failed", "message": f"{name} could not be completed."}

        if self._call_ended or self._task is None:
            # The decision above, made visible. This is the only record that
            # the work happened, so it carries the arguments too.
            logger.warning(
                f"[JOB] {name} finished AFTER the caller hung up — nobody was told. "
                f"arguments={arguments} result={result}"
            )
            return

        logger.info(f"[JOB] {name} finished, telling the caller")
        await self._announce(name, result)

    async def _announce(self, name: str, result: dict[str, Any]) -> None:
        """Put the outcome into the conversation and let the model say it."""
        outcome = "completed" if result.get("ok") else "failed"
        note = (
            f"The background task '{name}' has just {outcome}. Result: {result}. "
            f"Tell the caller this outcome now, briefly and naturally, as an "
            f"interruption to whatever is currently being discussed. If it failed, "
            f"say plainly what did not happen rather than glossing over it."
        )
        try:
            await self._task.queue_frame(
                LLMMessagesAppendFrame(messages=[{"role": "system", "content": note}], run_llm=True)
            )
        except Exception as e:
            # A call that ended between the check above and this line. Not
            # worth an error: the outcome is already in the log.
            logger.warning(f"[JOB] Could not announce {name}: {type(e).__name__}: {e}")

    def shutdown(self) -> None:
        """Called when the caller hangs up.

        Deliberately does not cancel anything — see the module docstring. It
        stops announcements (there is nobody to announce to) and lets each
        job finish and log itself.
        """
        self._call_ended = True
        if self._running:
            logger.warning(
                f"[JOB] Call ended with {len(self._running)} job(s) still running. "
                f"They will finish and log their result; nobody will hear it."
            )


# Appended to the system prompt only when a bot actually has a long-running
# tool, so bots without one do not carry the instruction as prompt noise.
BACKGROUND_TOOL_RULE = (
    "\n\nSome of your tools take a while and return immediately with an "
    "acknowledgement rather than an answer. When one does, tell the caller you "
    "are working on it and carry on the conversation — do not wait in silence "
    "and do not pretend it has finished. The real result will arrive shortly and "
    "you should mention it then, even if the conversation has moved on."
)
