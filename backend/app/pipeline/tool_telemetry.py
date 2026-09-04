"""Task 3.2 — seeing what several tools running at once actually did.

Pipecat 1.7 already runs a turn's tool calls concurrently
(`LLMService.run_in_parallel` defaults to True), so "make them parallel" was
done before this task started. What was missing is everything around it:
knowing which of them was slow, and — the part the manual singles out —
handling the case where one succeeds and another fails.

Both are observation problems, so this is an observer rather than a change
to the tools themselves. Wrapping each handler would mean touching the
signatures pipecat introspects to build a schema; hooking the events it
already emits does not.

On the pitfall, worth stating plainly because it is easy to get backwards:
when the cab is booked and the text message fails, "all done" is a lie and
"sorry, that failed" is also a lie, and one of them produces a customer who
believes they have no cab. The model can only report that accurately if
each tool result says what happened to *it*, which is why every result in
tool_registry carries `ok` and a `message` written to be spoken, and why
the system prompt tells the model to report each outcome separately.
"""

import time

from loguru import logger


class ToolCallTimer:
    """Logs how long each tool in a turn took, and how they finished.

    One instance per call. Registered against the LLM service's
    `on_function_calls_started` event, which fires once per turn with every
    call the model asked for — so the count alone tells you whether the
    model is using tools in parallel at all.
    """

    def __init__(self):
        self._started: dict[str, float] = {}

    async def on_calls_started(self, service, function_calls) -> None:
        """Note the moment a batch of calls begins."""
        names = [getattr(c, "function_name", "?") for c in function_calls]
        now = time.monotonic()
        for call in function_calls:
            key = getattr(call, "tool_call_id", None) or getattr(call, "function_name", "?")
            self._started[key] = now

        if len(names) > 1:
            # The interesting case: these run concurrently, so the turn should
            # cost about as long as the slowest one, not the sum.
            logger.info(f"[TOOLS] {len(names)} calls in parallel: {', '.join(names)}")
        elif names:
            logger.info(f"[TOOLS] calling {names[0]}")

    def finished(self, call_id: str, name: str, result) -> None:
        """Record one call's outcome. Called from the wrapped result callback."""
        started = self._started.pop(call_id, None)
        elapsed = f"{(time.monotonic() - started) * 1000:.0f}ms" if started else "?"

        ok = True
        if isinstance(result, dict) and "ok" in result:
            ok = bool(result["ok"])

        if ok:
            logger.info(f"[TOOLS] {name} ok in {elapsed}")
        else:
            detail = result.get("error", "failed") if isinstance(result, dict) else "failed"
            # Deliberately a warning, not an error: a tool failing is a normal
            # outcome the bot is expected to talk about, not an incident.
            logger.warning(f"[TOOLS] {name} FAILED in {elapsed} ({detail})")


# Appended to every bot's system prompt. Without it the model tends to
# summarise a batch of calls as one outcome, which is exactly the failure the
# manual warns about.
PARTIAL_FAILURE_RULE = (
    "\n\nWhen you use more than one tool at once, report what happened to each "
    "one separately. If one succeeded and another failed, say precisely that — "
    "name what worked and what did not, and never round a mixed result up to "
    "'all done' or down to 'that failed'. If a tool result says it could not "
    "reach a system, tell the caller that rather than inventing an answer."
)
