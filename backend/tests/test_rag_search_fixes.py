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
        self.behaviour = behaviour
        self.calls = 0

    def rerank(self, **kwargs):
        self.calls += 1
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

    def install(behaviour):
        inference = _Inference(behaviour)
        monkeypatch.setattr(rag, "_pc", types.SimpleNamespace(inference=inference))
        return inference

    yield install
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

    pinecone(slow_rerank)
    lines, sink = _capture_logs()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(rag.query_context("bot-1", "page 50"), timeout=0.1)
    finally:
        logger.remove(sink)

    timing = [line for line in lines if "search timing" in line]
    assert timing, "a search cancelled by the budget logged nothing"
    assert "stopped during rerank" in timing[-1]
    assert "embed=" in timing[-1] and "query=" in timing[-1]
