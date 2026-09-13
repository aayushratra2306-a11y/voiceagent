"""Live call 2026-09-13: Nitya could not say what was on page 50 of its PDF.

The server log for that one call shows three separate failures stacked up.

1. The page number never reached the search. The caller said, with short
   pauses, three things, and they arrived as three separate user messages
   with no reply in between:

       6 user -> What about I have uploaded a PDF.
       7 user -> Please tell me what's there on the page fifty of the PDF?
       8 user -> The headline of that particular page.

   The search read only the LAST user message, so it searched for "headline
   of that page" with no page filter and returned pages 40, 41, 5 and 11.

2. Every search ran past the 3.5s budget, so Nitya answered WITHOUT the
   document ("I don't have the ability to view the PDF"). Part of that time
   was a rerank request Pinecone refused outright:

       [RAG] Rerank failed ... 429 RESOURCE_EXHAUSTED ... reached the rerank
       request limit (500) for model bge-reranker-v2-m3 for the current month

   Because each call runs in a fresh process, every call paid for that
   refused request again.

3. The allowance was being spent twice per call: the per-call connection
   warm-up ran a full search, rerank included, on the words "warm up".

And the log could not say which step was slow, because a search cut off by
the budget logged nothing about where it had got to.

No real Pinecone, OpenAI or database is touched in this file.
"""

import asyncio
import types
from pathlib import Path

import pytest
from loguru import logger

from app.core import breaker
from app.pipeline import rag_processor as rp
from app.services import rag

pytestmark = pytest.mark.asyncio(loop_scope="session")

LIVE_MESSAGES = [
    {"role": "system", "content": "prompt"},
    {"role": "assistant", "content": "Hello! I'm ready. How can I help you?"},
    {"role": "user", "content": "Hello? Can you please tell me current date and time?"},
    {"role": "assistant", "tool_calls": [{"id": "1"}]},
    {"role": "tool", "content": '{"human_readable": "..."}'},
    {"role": "assistant", "content": "Sure thing. It's Sunday, 13 September 2026, 8 07 pm India time."},
    {"role": "user", "content": "What about I have uploaded a PDF."},
    {"role": "user", "content": "Please tell me what's there on the page fifty of the PDF?"},
    {"role": "user", "content": "The headline of that particular page."},
]


# --- (d) which text is searched -----------------------------------------------

def test_every_user_message_since_the_last_reply_is_searched_together():
    """The live failure. "page fifty" was in message 7; only 8 was searched."""
    text = rp.pending_user_text(LIVE_MESSAGES)

    assert "page fifty" in text
    assert text == ("What about I have uploaded a PDF. "
                    "Please tell me what's there on the page fifty of the PDF? "
                    "The headline of that particular page.")


def test_a_reply_in_between_starts_a_fresh_turn():
    """Messages the bot has already answered are not searched again."""
    messages = [
        {"role": "user", "content": "what is on page 20?"},
        {"role": "assistant", "content": "Page 20 covers hooks."},
        {"role": "user", "content": "and page 21?"},
    ]
    assert rp.pending_user_text(messages) == "and page 21?"


def test_a_tool_round_trip_after_the_question_does_not_hide_it():
    messages = [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "what time is it"},
        {"role": "assistant", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": '{"time": "12:00"}'},
    ]
    assert rp.pending_user_text(messages) == "what time is it"


def test_a_caller_talking_over_a_silent_bot_is_capped():
    """Unbounded joining would turn a long stretch of unanswered speech into
    one enormous search. The most recent few messages are what matter."""
    messages = [{"role": "assistant", "content": "hi"}] + [
        {"role": "user", "content": f"part {i}"} for i in range(1, 7)
    ]
    assert rp.pending_user_text(messages) == "part 4 part 5 part 6"


@pytest.mark.parametrize(("messages", "expected"), [
    ([{"role": "system", "content": "..."}, {"role": "assistant", "content": "hi"}], None),
    ([], None),
    ([{"role": "user", "content": []}], None),
    ([{"role": "user", "content": [{"type": "text", "text": "what does"},
                                   {"type": "text", "text": "the doc say"}]}], "what does the doc say"),
    ([{"role": "user", "content": "   "}, {"role": "user", "content": "page 4"}], "page 4"),
])
def test_edge_cases(messages, expected):
    assert rp.pending_user_text(messages) == expected


async def test_the_processor_searches_the_whole_pending_turn(monkeypatch):
    """End to end through the real processor: what reaches the rewriter is
    the joined turn, so "fifty" can become a page filter."""
    from pipecat.frames.frames import LLMContextFrame
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    async def _noop(self, frame, direction):
        return None

    monkeypatch.setattr(FrameProcessor, "process_frame", _noop)
    seen = {}

    async def fake_rewrite(text):
        seen["rewrite_input"] = text
        return "headline page 50"

    async def fake_query(bot_id, query, **kwargs):
        seen["query"] = query
        return "", []

    monkeypatch.setattr(rp, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(rp, "query_context", fake_query)

    processor = rp.RAGContextProcessor("bot-1", LLMContext(list(LIVE_MESSAGES)), "prompt")
    pushed = []

    async def capture(frame, direction=None):
        pushed.append(frame)

    processor.push_frame = capture
    context = LLMContext(list(LIVE_MESSAGES))
    await processor.process_frame(LLMContextFrame(context=context), FrameDirection.DOWNSTREAM)

    assert "page fifty" in seen["rewrite_input"]
    assert seen["query"] == "headline page 50"
    assert pushed, "the turn was not passed on to the LLM"


# --- fakes for the Pinecone side ----------------------------------------------

def _match(i, page):
    return types.SimpleNamespace(
        id=f"doc_{i}", score=0.3,
        metadata={"text": f"[Page {page}] chunk {i}", "page": float(page), "doc_id": "doc"},
    )


class _Index:
    def query(self, **kwargs):
        page = (kwargs.get("filter") or {}).get("page", {}).get("$eq", 40)
        return types.SimpleNamespace(matches=[_match(i, page) for i in range(3)])


class _Inference:
    def __init__(self, behaviour):
        import threading
        self.behaviour = behaviour
        self.calls = 0
        # Set the moment a rerank request is actually being made, so a test
        # can cancel the search DURING rerank rather than after a guessed
        # delay (a fixed 0.1s was not always enough on a busy machine).
        self.started = threading.Event()

    def rerank(self, **kwargs):
        self.calls += 1
        self.started.set()
        return self.behaviour(kwargs)


def _quota_refusal(_kwargs):
    raise Exception("(429) [429 RESOURCE_EXHAUSTED] Request failed. You've reached the rerank "
                    "request limit (500) for model bge-reranker-v2-m3 for the current month")


def _network_blip(_kwargs):
    raise ConnectionError("connection reset by peer")


def _good_rerank(kwargs):
    docs = kwargs["documents"]
    return types.SimpleNamespace(data=[
        types.SimpleNamespace(score=0.9 - n * 0.1, document={"text": t}) for n, t in enumerate(docs)
    ][: kwargs["top_n"]])


@pytest.fixture
def pinecone(monkeypatch, tmp_path: Path):
    """A fake index, embedder and reranker, and a breaker store of its own."""
    breaker.use_database(tmp_path / "breakers.db")
    breaker._configs.clear()

    async def fake_embed(texts):
        return [[0.0] * 3 for _ in texts]

    async def fake_embed_sparse(texts, input_type):
        return [{"indices": [1], "values": [1.0]} for _ in texts]

    monkeypatch.setattr(rag, "_get_index", lambda: _Index())
    monkeypatch.setattr(rag, "_get_sparse_index", lambda: _Index())
    monkeypatch.setattr(rag, "_embed", fake_embed)
    monkeypatch.setattr(rag, "_embed_sparse", fake_embed_sparse)

    import threading
    in_flight = [0]
    lock = threading.Lock()
    real_rerank_and_record = rag._rerank_and_record

    def counted_rerank_and_record(*args):
        # Counted around the WHOLE thread body, breaker write included: a
        # count taken inside the fake rerank drops to zero before the
        # outcome is recorded.
        with lock:
            in_flight[0] += 1
        try:
            return real_rerank_and_record(*args)
        finally:
            with lock:
                in_flight[0] -= 1

    monkeypatch.setattr(rag, "_rerank_and_record", counted_rerank_and_record)

    def install(behaviour):
        inference = _Inference(behaviour)
        client = types.SimpleNamespace(inference=inference)
        monkeypatch.setattr(rag, "_pc", client)
        monkeypatch.setattr(rag, "_get_rerank_client", lambda: client)
        return inference

    yield install
    # A rerank the search stopped waiting for is still running in its thread.
    # Left alone, it records into the NEXT test's breaker store when it
    # finishes (use_database is process-wide), which can flip that test's
    # breaker. Wait for it here, while this test's store is still the live one.
    import time as _time
    deadline = _time.monotonic() + 5
    while in_flight[0] and _time.monotonic() < deadline:
        _time.sleep(0.02)
    breaker._configs.clear()


# --- (a) the warm-up ------------------------------------------------------------

async def test_the_warm_up_search_spends_no_rerank_request(pinecone):
    inference = pinecone(_good_rerank)

    context, sources = await rag.query_context("bot-1", "warm up", rerank=False)

    assert inference.calls == 0, "a connection warm-up used up a rerank request"
    assert context, "the warm-up still has to exercise the real search path"


def test_the_call_warm_up_asks_for_no_rerank():
    source = (Path(rag.__file__).resolve().parents[1] / "pipeline" / "voice_pipeline.py").read_text(
        encoding="utf-8")
    assert 'query_context(bot_id, "warm up", rerank=False)' in source


async def test_a_real_search_still_reranks(pinecone):
    inference = pinecone(_good_rerank)

    _context, sources = await rag.query_context("bot-1", "headline page 50")

    assert inference.calls == 1
    assert sources and sources[0]["score"] == pytest.approx(0.9)
    assert sources[0]["page"] == 50, "the page filter from the query did not reach the index"


# --- (b) remembering a used-up allowance ------------------------------------------

async def test_after_a_quota_refusal_later_searches_skip_the_reranker(pinecone):
    inference = pinecone(_quota_refusal)

    first = await rag.query_context("bot-1", "page 50")
    second = await rag.query_context("bot-1", "page 50")

    assert inference.calls == 1, "a refused allowance was asked for again on the next search"
    assert first[0] and second[0], "search must still return raw-similarity results"
    assert all(s["score"] is None for s in second[1]), "unranked results must not carry a score"


async def test_the_used_up_allowance_is_remembered_by_the_shared_breaker(pinecone):
    """Each call is its own process, so an in-memory flag would be forgotten
    by the next call. The shared breaker store is what every call reads."""
    pinecone(_quota_refusal)

    await rag.query_context("bot-1", "page 50")

    assert breaker.state(rag.RERANK_BREAKER) == "open"


async def test_an_ordinary_network_error_does_not_switch_reranking_off(pinecone):
    inference = pinecone(_network_blip)

    await rag.query_context("bot-1", "page 50")
    await rag.query_context("bot-1", "page 50")

    assert inference.calls == 2, "a one-off network error disabled reranking"


async def test_after_the_cooldown_one_trial_rerank_is_let_through(pinecone, monkeypatch):
    inference = pinecone(_quota_refusal)
    await rag.query_context("bot-1", "page 50")

    real_time = breaker.time.time
    monkeypatch.setattr(breaker.time, "time", lambda: real_time() + rag.RERANK_QUOTA_COOLDOWN_SECONDS + 5)
    inference.behaviour = _good_rerank
    _context, sources = await rag.query_context("bot-1", "page 50")

    assert inference.calls == 2, "reranking never came back after the cooldown"
    assert sources[0]["score"] is not None


# --- (c) timing that survives the budget -------------------------------------------

async def _cancel_during_rerank(inference):
    """What the caller's budget does (asyncio.wait_for cancels the search),
    timed to land while the rerank request is in flight."""
    task = asyncio.create_task(rag.query_context("bot-1", "page 50"))
    started = await _eventually(inference.started.is_set)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert started, "the search never reached rerank"


def _capture_logs():
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="INFO")
    return lines, sink


async def test_a_completed_search_logs_each_step(pinecone):
    pinecone(_good_rerank)
    lines, sink = _capture_logs()
    try:
        await rag.query_context("bot-1", "page 50")
    finally:
        logger.remove(sink)

    timing = [line for line in lines if "search timing" in line]
    assert timing, "no timing line for a completed search"
    assert all(step in timing[-1] for step in ("embed=", "query=", "rerank=", "total=", "completed"))


async def test_a_search_cut_off_by_the_budget_still_says_where_it_was(pinecone):
    """The live log said only "exceeded 3.5s budget". Without the step it
    stopped in, the fix for the delay would be a guess."""
    import time as _time

    def slow_rerank(kwargs):
        _time.sleep(0.5)
        return _good_rerank(kwargs)

    inference = pinecone(slow_rerank)
    lines, sink = _capture_logs()
    try:
        await _cancel_during_rerank(inference)
    finally:
        logger.remove(sink)

    timing = [line for line in lines if "search timing" in line]
    assert timing, "a search cancelled by the budget logged nothing"
    assert "stopped during rerank" in timing[-1]
    assert "embed=" in timing[-1] and "query=" in timing[-1]


# --- (e) a slow rerank inside the budget -----------------------------------------
#
# Retest 2026-09-13, after (a)-(d) were deployed. The rewrite worked
# ("page fifty" -> "page 50") and embed + query took 1.2s, then:
#
#     search timing: embed=0.63s query=0.57s rerank=- total=3.15s (stopped during rerank)
#     Retrieval exceeded 3.5s budget ... answering without document context
#
# Two things went wrong at once:
#
# 1. The rerank step had no deadline of its own, so a slow rerank did not
#    just lose the ranking: the 3.5s budget threw away the whole search,
#    page-50 results included, which were already in hand.
# 2. The budget cancels by raising CancelledError at the await, which skips
#    the except block that records a quota refusal. The breaker from (b)
#    could therefore never open on exactly the slow refusal it exists for,
#    and every later search would pay the same delay again.
#
# Why the refusal was slow at all: the Pinecone SDK (9.1) retries a 429 three
# times, sleeping between attempts, inside the worker thread, where nothing
# can cancel it. A MONTHLY allowance does not come back in a few seconds.

def _slow(behaviour, seconds):
    import time as _time

    def run(kwargs):
        _time.sleep(seconds)
        return behaviour(kwargs)
    return run


async def _eventually(check, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def test_a_slow_rerank_keeps_the_results_it_already_found(pinecone, monkeypatch):
    """The live failure: page-50 chunks were found, then discarded because
    the reranker was slow. They must reach the answer, unranked."""
    monkeypatch.setattr(rag, "RERANK_TIMEOUT_SECONDS", 0.2)
    pinecone(_slow(_good_rerank, 0.8))

    started = asyncio.get_running_loop().time()
    context, sources = await rag.query_context("bot-1", "headline page 50")
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.6, f"waited {elapsed:.2f}s for a reranker past its deadline"
    assert "[Page 50]" in context, "the results already found were thrown away"
    assert sources and all(s["score"] is None for s in sources)


async def test_a_slow_rerank_is_named_in_the_timing_line(pinecone, monkeypatch):
    monkeypatch.setattr(rag, "RERANK_TIMEOUT_SECONDS", 0.2)
    pinecone(_slow(_good_rerank, 0.8))
    lines, sink = _capture_logs()
    try:
        await rag.query_context("bot-1", "page 50")
    finally:
        logger.remove(sink)

    timing = [line for line in lines if "search timing" in line][-1]
    assert "rerank=timed out" in timing and "completed" in timing


async def test_a_quota_refusal_that_arrives_after_the_deadline_still_opens_the_breaker(
    pinecone, monkeypatch
):
    monkeypatch.setattr(rag, "RERANK_TIMEOUT_SECONDS", 0.1)
    pinecone(_slow(_quota_refusal, 0.4))

    await rag.query_context("bot-1", "page 50")

    assert await _eventually(lambda: breaker.state(rag.RERANK_BREAKER) == "open"), (
        "the refusal came back after the search moved on and was never recorded"
    )


async def test_a_refusal_cut_off_by_the_whole_budget_still_opens_the_breaker(pinecone):
    """Exactly the live shape: the CALLER's budget cancels the search while
    the refusal is still on its way back."""
    inference = pinecone(_slow(_quota_refusal, 0.4))

    await _cancel_during_rerank(inference)

    assert await _eventually(lambda: breaker.state(rag.RERANK_BREAKER) == "open"), (
        "cancellation skipped recording the refusal, so the next call pays for it again"
    )


async def test_a_late_network_error_does_not_switch_reranking_off(pinecone, monkeypatch):
    monkeypatch.setattr(rag, "RERANK_TIMEOUT_SECONDS", 0.1)
    inference = pinecone(_slow(_network_blip, 0.3))

    await rag.query_context("bot-1", "page 50")
    await asyncio.sleep(0.5)

    assert breaker.state(rag.RERANK_BREAKER) != "open"
    inference.behaviour = _good_rerank
    await rag.query_context("bot-1", "page 50")
    assert inference.calls == 2


async def test_an_abandoned_rerank_does_not_log_an_unretrieved_exception(pinecone, monkeypatch):
    """A future nobody waits for any more still finishes. If its exception is
    never read, asyncio reports it as an error in the log when it is garbage
    collected, which would look like a crash in the middle of a call."""
    import gc

    monkeypatch.setattr(rag, "RERANK_TIMEOUT_SECONDS", 0.1)
    pinecone(_slow(_network_blip, 0.3))
    loop = asyncio.get_running_loop()
    reported = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        await rag.query_context("bot-1", "page 50")
        await asyncio.sleep(0.5)
        gc.collect()
        await asyncio.sleep(0.05)
    finally:
        loop.set_exception_handler(previous)

    assert not reported, reported


def test_the_reranker_does_not_retry_a_refusal(monkeypatch):
    """The SDK's default is 3 retries with sleeps in between, all inside a
    thread nothing can cancel. For rerank that bought nothing (a fallback
    exists) and cost the budget."""
    built = {}

    class FakePinecone:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr(rag, "Pinecone", FakePinecone)
    monkeypatch.setattr(rag, "_rerank_pc", None)

    rag._get_rerank_client()

    assert built["retry_config"].max_retries == 0


# --- (e, review) bounding the abandoned request, and the time actually left ----
#
# Independent review of the fix above, 2026-09-13:
# - an abandoned rerank kept the SDK's 30s request timeout, holding one of the
#   few default thread-pool threads (min(32, cpus + 4)) that embed, the index
#   queries and breaker reads also wait on;
# - a fixed 1.2s deadline ignores how much of the 3.5s budget is left, so slow
#   earlier steps plus 1.2s of rerank could still lose the results.

def test_an_abandoned_rerank_cannot_hold_a_thread_for_long(monkeypatch):
    built = {}

    class FakePinecone:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr(rag, "Pinecone", FakePinecone)
    monkeypatch.setattr(rag, "_rerank_pc", None)

    rag._get_rerank_client()

    assert built["timeout"] <= 3.0, "an abandoned rerank could hold a worker thread for the SDK's 30s"
    assert built["timeout"] > rag.RERANK_TIMEOUT_SECONDS, "the request would die before the search stops waiting"


async def test_no_rerank_is_sent_when_the_budget_is_already_spent(pinecone):
    inference = pinecone(_good_rerank)
    lines, sink = _capture_logs()
    try:
        context, sources = await rag.query_context(
            "bot-1", "page 50", deadline=asyncio.get_running_loop().time() - 0.1
        )
    finally:
        logger.remove(sink)

    assert inference.calls == 0, "a rerank that could not come back in time still spent the allowance"
    assert "[Page 50]" in context and all(s["score"] is None for s in sources)
    timing = [line for line in lines if "search timing" in line][-1]
    assert "rerank=skipped (no time left)" in timing


async def test_rerank_waits_only_for_the_time_actually_left(pinecone, monkeypatch):
    monkeypatch.setattr(rag, "RERANK_TIMEOUT_SECONDS", 5.0)
    pinecone(_slow(_good_rerank, 0.8))
    loop = asyncio.get_running_loop()

    started = loop.time()
    context, _sources = await rag.query_context("bot-1", "page 50", deadline=started + 0.4)
    elapsed = loop.time() - started

    assert elapsed < 0.7, f"rerank ran {elapsed:.2f}s past the caller's deadline"
    assert "[Page 50]" in context


async def test_the_processor_hands_its_deadline_to_the_search(monkeypatch):
    from pipecat.frames.frames import LLMContextFrame
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    async def _noop(self, frame, direction):
        return None

    monkeypatch.setattr(FrameProcessor, "process_frame", _noop)
    seen = {}

    async def fake_rewrite(text):
        return "page 50"

    async def fake_query(bot_id, query, **kwargs):
        seen.update(kwargs)
        return "", []

    monkeypatch.setattr(rp, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(rp, "query_context", fake_query)
    processor = rp.RAGContextProcessor("bot-1", LLMContext(list(LIVE_MESSAGES)), "prompt")

    async def capture(frame, direction=None):
        return None

    processor.push_frame = capture
    before = asyncio.get_running_loop().time()
    await processor.process_frame(
        LLMContextFrame(context=LLMContext(list(LIVE_MESSAGES))), FrameDirection.DOWNSTREAM
    )

    assert "deadline" in seen, "the search was not told how long it has"
    assert before < seen["deadline"] < before + rp.RETRIEVAL_BUDGET_SECONDS, (
        "the deadline must leave room to build the answer inside the budget"
    )


async def test_a_rerank_that_answers_late_is_logged_with_its_time(pinecone, monkeypatch):
    """Whether 1.2s is the right deadline is not yet measured (the quota ran
    out before timing was logged). Late answers are the evidence, so each
    one says how long it actually took."""
    monkeypatch.setattr(rag, "RERANK_TIMEOUT_SECONDS", 0.1)
    pinecone(_slow(_good_rerank, 0.3))
    lines, sink = _capture_logs()
    try:
        await rag.query_context("bot-1", "page 50")
        assert await _eventually(lambda: any("answered late" in line for line in lines))
    finally:
        logger.remove(sink)

    late = [line for line in lines if "answered late" in line][-1]
    assert "after 0." in late


async def test_a_search_with_no_time_left_does_not_use_up_the_one_trial(pinecone, monkeypatch):
    """After the cooldown, breaker.allows() hands exactly ONE caller the trial
    and records that it did. A search that then declined to send the rerank
    for lack of time would strand that trial, and reranking would stay off
    for another full hour."""
    inference = pinecone(_quota_refusal)
    await rag.query_context("bot-1", "page 50")
    assert breaker.state(rag.RERANK_BREAKER) == "open"

    real_time = breaker.time.time
    monkeypatch.setattr(breaker.time, "time", lambda: real_time() + rag.RERANK_QUOTA_COOLDOWN_SECONDS + 5)
    inference.behaviour = _good_rerank
    await rag.query_context("bot-1", "page 50", deadline=asyncio.get_running_loop().time() - 0.1)
    _context, sources = await rag.query_context("bot-1", "page 50")

    assert inference.calls == 2, "the post-cooldown trial was used up by a search that never sent it"
    assert sources[0]["score"] is not None
