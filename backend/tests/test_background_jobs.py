"""Task 3.3 — work that outlasts the turn that started it.

Some APIs take five or ten seconds. On a phone call that is unbearable
silence, so a tool marked long-running returns an acknowledgement at once
and speaks its real result when it arrives.

The case these tests care most about is the one the manual singles out: the
caller hangs up while a job is still running. The decision made here is
that the job is NOT cancelled — cancelling a request that may already have
booked something is how a remote system's state becomes unknowable — so it
finishes and logs itself loudly, because that log is then the only record
that it ever happened.
"""

import asyncio

import pytest

from app.models.bot_tool import BotTool
from app.pipeline.background_jobs import BACKGROUND_TOOL_RULE, BackgroundJobs
from app.services.tool_registry import _http_handler

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _Task:
    """Stands in for the pipeline task, capturing what would be spoken."""

    def __init__(self, fail=False):
        self.frames, self.fail = [], fail

    async def queue_frame(self, frame):
        if self.fail:
            raise RuntimeError("pipeline already closed")
        self.frames.append(frame)


class _Params:
    """Stands in for pipecat's function-call params."""

    def __init__(self, **args):
        self.arguments, self.result = args, None

    async def result_callback(self, result):
        self.result = result


async def _settle():
    """Let background tasks run to completion."""
    for _ in range(20):
        await asyncio.sleep(0.01)


def _tool(**over) -> BotTool:
    base = dict(bot_id="b", name="slow_lookup", description="d",
                url="https://slow.test/", long_running=True)
    base.update(over)
    return BotTool(**base)


# --- the acknowledgement ---------------------------------------------------

async def test_a_long_running_tool_returns_before_the_work_finishes():
    """The point of the task: the caller hears something immediately rather
    than ten seconds of silence."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow():
        started.set()
        await release.wait()
        return {"ok": True, "data": "done"}

    jobs = BackgroundJobs(_Task())
    params = _Params()

    handler = _http_handler(_tool(), jobs)

    # Patch the work itself rather than the network, so timing is exact.
    import app.services.tool_registry as reg
    original = reg.call_http_tool
    reg.call_http_tool = lambda tool, args: slow()
    try:
        await asyncio.wait_for(handler(params), timeout=0.5)
        assert params.result is not None, "the handler did not answer the model"
        assert params.result["started"] is True
        assert params.result["ok"] is True
        assert not release.is_set(), "the handler waited for the work after all"
        assert jobs.in_flight == 1
        release.set()
        await _settle()
    finally:
        reg.call_http_tool = original


async def test_the_acknowledgement_tells_the_model_not_to_claim_success():
    """"Started" and "done" are different things, and a model told only
    `ok: True` will happily say the second."""
    jobs = BackgroundJobs(_Task())
    params = _Params()

    import app.services.tool_registry as reg
    original = reg.call_http_tool
    reg.call_http_tool = lambda tool, args: asyncio.sleep(0, {"ok": True})
    try:
        await _http_handler(_tool(), jobs)(params)
        message = params.result["message"].lower()
        assert "do not say it is done" in message or "not" in message
        assert "carry on" in message
    finally:
        reg.call_http_tool = original
        await _settle()


async def test_a_normal_tool_is_untouched_by_any_of_this():
    """Only tools explicitly marked long-running take the new path."""
    jobs = BackgroundJobs(_Task())
    params = _Params()

    import app.services.tool_registry as reg
    original = reg.call_http_tool

    async def immediate(tool, args):
        return {"ok": True, "data": "straight away"}

    reg.call_http_tool = immediate
    try:
        await _http_handler(_tool(long_running=False), jobs)(params)
        assert params.result["data"] == "straight away"
        assert "started" not in params.result
        assert jobs.in_flight == 0
    finally:
        reg.call_http_tool = original


# --- the result comes back into the conversation ---------------------------

async def test_the_result_is_injected_so_the_model_can_speak_it():
    task = _Task()
    jobs = BackgroundJobs(task)

    async def work():
        return {"ok": True, "data": {"stock": 4}}

    jobs.start("check_stock", work(), {"sku": "A1"})
    await _settle()

    assert len(task.frames) == 1, "nothing was said to the caller"
    frame = task.frames[0]
    assert frame.run_llm is True, "the message was appended but no reply triggered"
    content = frame.messages[0]["content"]
    assert "check_stock" in content and "completed" in content


async def test_a_failed_job_is_announced_as_a_failure():
    """Silence after "I'm looking that up" is worse than bad news."""
    task = _Task()
    jobs = BackgroundJobs(task)

    async def work():
        return {"ok": False, "error": "timeout"}

    jobs.start("check_stock", work(), {})
    await _settle()

    content = task.frames[0].messages[0]["content"]
    assert "failed" in content
    assert "plainly" in content


async def test_a_job_that_raises_still_reports_something():
    """An exception in a background task is invisible by default — asyncio
    swallows it — which would leave the caller waiting forever."""
    task = _Task()
    jobs = BackgroundJobs(task)

    async def work():
        raise RuntimeError("boom")

    jobs.start("check_stock", work(), {})
    await _settle()

    assert task.frames, "an exception left the caller with no answer at all"
    assert "failed" in task.frames[0].messages[0]["content"]


# --- the hang-up case, which the manual singles out ------------------------

async def test_a_job_finishing_after_the_hang_up_says_nothing_and_logs_loudly(monkeypatch):
    """Nobody is listening, so the log is the only record it happened — and
    it has to carry the arguments, or the record is useless."""
    warnings = []
    monkeypatch.setattr("app.pipeline.background_jobs.logger.warning", lambda m: warnings.append(m))

    task = _Task()
    jobs = BackgroundJobs(task)
    release = asyncio.Event()

    async def work():
        await release.wait()
        return {"ok": True, "data": "booked"}

    jobs.start("book_cab", work(), {"pickup": "Andheri"})
    jobs.shutdown()          # the caller hangs up
    release.set()
    await _settle()

    assert task.frames == [], "spoke into a call that had already ended"
    record = [w for w in warnings if "AFTER the caller hung up" in w]
    assert record, warnings
    assert "Andheri" in record[0], "the record does not say what was actually done"


async def test_hanging_up_does_not_cancel_work_already_in_flight():
    """The deliberate decision: cancelling a request that may already have
    booked something leaves the remote system in an unknown state."""
    task = _Task()
    jobs = BackgroundJobs(task)
    completed = asyncio.Event()

    async def work():
        await asyncio.sleep(0.05)
        completed.set()
        return {"ok": True}

    jobs.start("book_cab", work(), {})
    jobs.shutdown()
    await _settle()

    assert completed.is_set(), "the in-flight request was cancelled — its outcome is now unknowable"


async def test_the_call_ending_between_check_and_speak_does_not_raise():
    """A genuine race: the job passes the ended check, then the pipeline
    closes before the frame is queued."""
    jobs = BackgroundJobs(_Task(fail=True))

    async def work():
        return {"ok": True}

    jobs.start("check_stock", work(), {})
    await _settle()          # must not raise


async def test_shutdown_with_nothing_running_is_quiet(monkeypatch):
    warnings = []
    monkeypatch.setattr("app.pipeline.background_jobs.logger.warning", lambda m: warnings.append(m))
    BackgroundJobs(_Task()).shutdown()
    assert warnings == []


# --- the prompt rule -------------------------------------------------------

def test_the_model_is_told_what_an_acknowledgement_means():
    rule = BACKGROUND_TOOL_RULE.lower()
    assert "acknowledgement" in rule
    assert "do not wait in silence" in rule
    assert "even if the conversation has moved on" in rule


def test_the_rule_is_only_added_when_the_bot_has_such_a_tool():
    """Prompt text costs tokens on every turn of every call. A bot with no
    long-running tool should not carry an explanation of them."""
    import inspect

    from app.pipeline import voice_pipeline

    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "if has_background:" in source
    assert "BACKGROUND_TOOL_RULE" in source


def test_the_pipeline_shuts_jobs_down_when_the_caller_leaves():
    import inspect

    from app.pipeline import voice_pipeline

    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "jobs.shutdown()" in source
    assert "jobs.attach(task)" in source, "a finished job would have nowhere to speak"
