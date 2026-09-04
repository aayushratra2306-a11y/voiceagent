"""Task 3.2 — several tools at once, and the mixed outcome that follows.

Pipecat 1.7 already runs a turn's tool calls concurrently
(`LLMService.run_in_parallel` defaults to True), so the task's first and
third steps were done before it started. Saying that plainly matters: the
work here is the consequences, not the concurrency.

Two consequences, and the second is the one the manual singles out:

  - Concurrency hides which tool was slow, because the turn now costs as
    long as the slowest rather than the sum. Per-call timing gets that back.
  - A mixed outcome stops being rare. When the cab is booked and the text
    message fails, "all done" is a lie and "sorry, that failed" is also a
    lie — and one of them leaves a customer believing they have no cab.
"""

import asyncio
import time

import pytest

from app.pipeline.tool_telemetry import PARTIAL_FAILURE_RULE, ToolCallTimer
from app.services import tool_registry
from app.services.tool_registry import call_http_tool

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _Call:
    """The shape pipecat hands to on_function_calls_started."""

    def __init__(self, name, call_id):
        self.function_name, self.tool_call_id = name, call_id


# --- the concurrency is pipecat's, but it must stay on ---------------------

def test_pipecat_still_runs_tool_calls_in_parallel():
    """A default, not a setting we pass — so a pipecat upgrade that flipped
    it would silently double the time of any two-tool turn with nothing to
    show for it. This is the tripwire."""
    import inspect

    from pipecat.services.llm_service import LLMService

    signature = inspect.signature(LLMService.__init__)
    assert signature.parameters["run_in_parallel"].default is True, (
        "pipecat no longer runs tool calls in parallel by default — a two-tool "
        "turn now costs the sum of both, and this project relies on it not doing that"
    )


async def test_two_slow_tools_together_cost_about_one(monkeypatch):
    """The property the task exists for, measured rather than assumed."""

    class _Slow:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, *a, **kw):
            await asyncio.sleep(0.25)

            class R:
                status_code, text = 200, "{}"
            return R()

    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: _Slow())

    from app.models.bot_tool import BotTool
    tool = BotTool(bot_id="b", name="t", description="d", url="https://x.test/")

    started = time.monotonic()
    await asyncio.gather(call_http_tool(tool, {}), call_http_tool(tool, {}))
    elapsed = time.monotonic() - started

    assert elapsed < 0.45, f"two 0.25s calls took {elapsed:.2f}s — they ran one after the other"


# --- timing ----------------------------------------------------------------

async def test_a_parallel_batch_is_logged_as_one(monkeypatch):
    """Whether the model actually used two tools at once should be readable
    from a log line, not inferred from timestamps."""
    lines = []
    monkeypatch.setattr("app.pipeline.tool_telemetry.logger.info", lambda m: lines.append(m))

    timer = ToolCallTimer()
    await timer.on_calls_started(None, [_Call("book_cab", "1"), _Call("send_sms", "2")])

    assert any("2 calls in parallel" in m for m in lines), lines
    assert any("book_cab" in m and "send_sms" in m for m in lines)


async def test_each_tool_reports_its_own_duration(monkeypatch):
    """Concurrency hides the slow one unless each is timed separately."""
    infos, warns = [], []
    monkeypatch.setattr("app.pipeline.tool_telemetry.logger.info", lambda m: infos.append(m))
    monkeypatch.setattr("app.pipeline.tool_telemetry.logger.warning", lambda m: warns.append(m))

    timer = ToolCallTimer()
    await timer.on_calls_started(None, [_Call("fast", "1"), _Call("slow", "2")])
    timer.finished("1", "fast", {"ok": True})
    timer.finished("2", "slow", {"ok": True})

    assert any("fast ok in" in m and "ms" in m for m in infos), infos
    assert any("slow ok in" in m for m in infos)


async def test_a_failed_tool_is_logged_as_a_warning_not_an_error(monkeypatch):
    """A tool failing is a normal outcome the bot talks about, not an
    incident — logging it as an error would train someone to ignore errors."""
    infos, warns = [], []
    monkeypatch.setattr("app.pipeline.tool_telemetry.logger.info", lambda m: infos.append(m))
    monkeypatch.setattr("app.pipeline.tool_telemetry.logger.warning", lambda m: warns.append(m))

    timer = ToolCallTimer()
    await timer.on_calls_started(None, [_Call("send_sms", "9")])
    timer.finished("9", "send_sms", {"ok": False, "error": "timeout"})

    assert any("send_sms FAILED" in m and "timeout" in m for m in warns), warns
    assert not any("send_sms ok" in m for m in infos)


def test_an_untimed_call_does_not_crash_the_logger():
    """A result arriving for a call the timer never saw — an interruption, a
    reconnect — must not raise inside a live conversation."""
    ToolCallTimer().finished("never-seen", "mystery", {"ok": True})


# --- the pitfall: partial failure -----------------------------------------

async def test_a_mixed_batch_leaves_each_result_speaking_for_itself(monkeypatch):
    """The model can only report a mixed outcome accurately if each result
    says what happened to *it*. One shared status would make that impossible."""

    class _Client:
        def __init__(self, fail):
            self.fail = fail

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, *a, **kw):
            if self.fail:
                raise tool_registry.httpx.TimeoutException("slow")

            class R:
                status_code, text = 200, '{"booked": true}'
            return R()

    from app.models.bot_tool import BotTool
    good = BotTool(bot_id="b", name="book_cab", description="d", url="https://ok.test/")
    bad = BotTool(bot_id="b", name="send_sms", description="d", url="https://bad.test/")

    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: _Client(False))
    booked = await call_http_tool(good, {})
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: _Client(True))
    texted = await call_http_tool(bad, {})

    assert booked["ok"] is True
    assert texted["ok"] is False
    # Not just a flag: something the bot can actually say out loud.
    assert texted["message"], "the failed tool gave the model nothing to say"


def test_the_model_is_told_not_to_round_a_mixed_result():
    """Left to itself the model summarises a batch as one outcome. This is
    the instruction that stops "all done" when the message failed."""
    rule = PARTIAL_FAILURE_RULE.lower()
    assert "separately" in rule
    assert "all done" in rule, "the exact failure mode is not named, so the model may still do it"


def test_the_rule_actually_reaches_the_system_prompt():
    """An instruction written but not appended is worse than none — it looks
    handled in review and does nothing at runtime."""
    import inspect

    from app.pipeline import voice_pipeline

    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "PARTIAL_FAILURE_RULE" in source


def test_the_timer_is_wired_to_pipecats_event():
    """Timing that is never registered measures nothing — the same way the
    VAD instrumentation shipped dead in 4e608da."""
    import inspect

    from app.pipeline import voice_pipeline

    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "on_function_calls_started" in source
    assert "ToolCallTimer()" in source
