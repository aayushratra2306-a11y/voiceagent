import asyncio
import re
import time

from loguru import logger
from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.models.document import Document
from app.services.rag import query_context, rewrite_query

# Latency (2026-09-03). A full retrieval cycle — rewrite, embed, hybrid
# search, rerank — measures about 2.0s, and it was running on EVERY user
# turn. Plenty of turns cannot possibly be answered from a document:
# "hello", "thanks", "sorry, say that again". Those paid the full 2s for a
# lookup guaranteed to return nothing.
#
# Deliberately a fixed word list rather than a classifier. Any model call
# smart enough to judge this would cost most of the latency being saved,
# and a wrong "skip" is a silent quality regression — the bot answering from
# general knowledge while the document sat right there. So this only skips
# when EVERY word is conversational filler: no nouns, no question words,
# nothing a document could speak to. Anything else, including anything
# unrecognised, falls through to full retrieval.
_FILLER_WORDS = frozenset("""
hello hi hey yo greetings namaste
bye goodbye cheers
thanks thank thankyou ok okay k kk right sure yeah yep yes yup no nope nah
alright fine cool great good nice perfect got gotcha understood
please sorry pardon excuse
um uh er hmm mmm ah oh well so like just really very
you your me my i am is are was the a an and or but to of it that this
can could would repeat again say said one moment second wait
there here morning afternoon evening night doing everyone mate buddy dear
""".split())

# Question words (what / how / why / when / where / who) are deliberately
# ABSENT. Under the all-words rule, adding them would let "what is that" or
# "how does it work" skip retrieval, and those are real follow-up questions
# about the document. Leaving them out means such a turn pays the 2s and
# gets a correct answer, which is the right way round to be wrong.

# Guard against a short utterance that IS a real question — "why?", "page 4",
# "the invoice" — by never skipping when a digit or a question mark is present.
_MEANINGFUL = re.compile(r"[0-9?]")

# How long retrieval may hold an LLMContextFrame before we give up and let
# the reply through uncited.
#
# BUG FOUND 2026-09-03 from live logs, ORIGINALLY fixed here on the same
# day. At the time this processor sat BEFORE the user aggregator, holding
# the raw transcript frame while it searched. The aggregator's own
# `user_turn_stop_timeout` (5.0s) would fire believing no transcript had
# arrived at all, and the bot said "Sorry, I didn't catch that" to a caller
# it had heard perfectly — measured instance: retrieval took 7.03s, the
# aggregator gave up at 4.1s.
#
# STILL RELEVANT, DIFFERENT REASON, as of the 2026-09-03 move to AFTER the
# aggregator (see latest_user_text's docstring): that specific race is now
# structurally impossible, because the aggregator has already committed the
# turn and moved on by the time this processor ever sees a frame — its own
# timeout can't fire for a turn it already finished. The budget stays
# because an unbounded Pinecone/OpenAI call would still hang the reply
# indefinitely otherwise; it's now a plain latency ceiling rather than a fix
# for a specific timing race. AGGREGATOR_TURN_STOP_TIMEOUT is kept as a
# documented reference point, not because this budget still races against
# it.
#
# Exceeding the budget degrades to answering from general knowledge — a
# slightly worse answer than a cited one, but far better than an unbounded
# wait.
RETRIEVAL_BUDGET_SECONDS = 3.5
AGGREGATOR_TURN_STOP_TIMEOUT = 5.0  # pipecat LLMUserAggregatorParams default


def needs_retrieval(text: str) -> bool:
    """False only when every word is conversational filler."""
    if _MEANINGFUL.search(text):
        return True
    words = re.findall(r"[a-z']+", text.lower())
    if not words:
        return False
    return not all(w in _FILLER_WORDS for w in words)


def latest_user_text(messages: list[dict]) -> str | None:
    """The text of the most recent user-role message, or None if there
    isn't one.

    Bug found 2026-09-03 from live logs, root-caused by reading pipecat's
    aggregator source rather than guessing. This processor used to sit
    BEFORE the user aggregator and react to every raw TranscriptionFrame
    from Deepgram directly — but Deepgram closes a "final" chunk out on
    any pause past its endpointing threshold, and pipecat pushes each of
    those unconditionally as its own frame with no merging. A single
    spoken sentence with one mid-thought pause could arrive as two or more
    separate fragments, each triggering its own document search and its
    own rewrite of the system prompt — searching on half a sentence, then
    doing it again for the other half.

    The aggregator (`LLMUserAggregator`) already solves exactly this: it
    buffers fragments and only commits real text to the conversation once
    pipecat's own turn-detector (VAD + the Smart Turn model) decides the
    user has actually finished. So this processor now moves to AFTER that
    aggregator and reacts to the `LLMContextFrame` it emits — the same
    signal that is about to trigger the LLM — and reads back whatever text
    the aggregator just committed, rather than a raw, possibly-partial
    transcript fragment.

    This does not guarantee a fully complete sentence every time — the
    turn-detector's own judgment about "are they done" is a separate,
    genuine limitation (see the note above RETRIEVAL_BUDGET_SECONDS's
    sibling in providers.py) — but it does guarantee this processor never
    searches on LESS text than the LLM itself is about to see, which is
    the part that was structurally broken.
    """
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Some LLM context implementations represent a message as a
            # list of typed parts (multimodal-style) rather than a plain
            # string. Join whatever text parts exist rather than silently
            # returning nothing for a message that does have real content.
            parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            joined = " ".join(p for p in parts if p)
            return joined or None
        return None
    return None


class RAGContextProcessor(FrameProcessor):
    """
    Sits between the user context aggregator and the LLM service.
    Fires once per real, aggregator-confirmed user turn (an
    `LLMContextFrame`) rather than on every raw STT fragment — see
    `latest_user_text`'s docstring for why that distinction matters.
    """

    def __init__(
        self,
        bot_id: str,
        llm_context: LLMContext,
        base_system_prompt: str,
        webrtc_connection=None,
    ):
        super().__init__()
        self._bot_id = bot_id
        self._context = llm_context
        self._base_prompt = base_system_prompt
        # Task 2.10 — the channel used to tell the browser which document
        # and page an answer came from. Optional so this processor stays
        # constructible in tests and in any non-WebRTC context.
        self._webrtc = webrtc_connection
        # doc_id -> (filename, has_file). Documents are immutable once
        # uploaded, and a RAG lookup happens on every single user turn, so
        # re-reading the same records from MongoDB mid-call would be pure
        # latency in the one path where latency is most visible.
        self._doc_cache: dict[str, tuple[str, bool]] = {}

    async def _publish_sources(self, sources: list[dict]) -> None:
        """Send the citations for this turn to the browser.

        Always sends, including when `sources` is empty — the manual's own
        point for task 2.10 is that "the bot answered from general knowledge,
        not your documents" is itself useful information, and silence would
        be indistinguishable from a dropped message.
        """
        if self._webrtc is None:
            return

        enriched = []
        for s in sources:
            doc_id = s.get("doc_id")
            cached = self._doc_cache.get(doc_id) if doc_id else None
            if cached is None and doc_id:
                try:
                    doc = await Document.get(doc_id)
                    # has_file is False for anything uploaded before Task
                    # 2.10 stored the original bytes — those still cite
                    # correctly, the frontend just can't offer to open them.
                    cached = (
                        (doc.filename, doc.file_id is not None)
                        if doc
                        else ("Unknown document", False)
                    )
                except Exception as e:
                    # A citation is a nice-to-have. Never let looking one up
                    # break a live call.
                    logger.warning(f"[RAG] Could not resolve document {doc_id}: {e}")
                    cached = ("Unknown document", False)
                self._doc_cache[doc_id] = cached
            filename, has_file = cached or ("Unknown document", False)
            enriched.append({
                "doc_id": doc_id,
                "filename": filename,
                "page": s.get("page"),
                "score": s.get("score"),
                "has_file": has_file,
            })

        try:
            # Not awaited — SmallWebRTCConnection.send_app_message is a plain
            # sync method (pipecat 1.7.0, connection.py:746). Awaiting it
            # raises TypeError on every turn, which the except below would
            # then log as a failure even though the message did go out.
            self._webrtc.send_app_message({"type": "rag-sources", "sources": enriched})
        except Exception as e:
            logger.warning(f"[RAG] Could not send sources to client: {e}")

    async def _retrieve(self, raw_text: str):
        """Rewrite then search. Separated so the pair can share one deadline —
        a budget on the search alone would still let a slow rewrite blow it."""
        t0 = time.perf_counter()
        search_query = await rewrite_query(raw_text)
        t_rewrite = time.perf_counter() - t0

        # Keep the raw transcript in the log even though search_query is what
        # actually gets used — per task 1.6, useful for spotting a rewrite that
        # went wrong without needing to reproduce it live.
        if search_query != raw_text:
            logger.info(f"[RAG] Query rewritten: '{raw_text}' -> '{search_query}'")
        else:
            logger.info(f"[RAG] Query (unchanged): '{raw_text}'")

        t1 = time.perf_counter()
        retrieved, sources = await query_context(self._bot_id, search_query)
        return search_query, retrieved, sources, t_rewrite, time.perf_counter() - t1

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)

        # Only the aggregator's own downstream commit — direction guard is
        # cheap insurance against reacting to an LLMContextFrame flowing the
        # other way (e.g. from the assistant-side aggregator further down
        # the pipeline), which this processor was never meant to see.
        is_real_turn = (
            isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM
        )
        raw_text = latest_user_text(frame.context.messages) if is_real_turn else None

        if raw_text and raw_text.strip():
            # Mutated on the frame's own context object, not self._context.
            # In this pipeline they are the same object (both processors
            # were handed the one LLMContext), but writing through the
            # frame is correct regardless of that — it's what the LLM
            # service downstream is about to read.
            messages = frame.context.messages

            if not needs_retrieval(raw_text):
                # Nothing to look up. Restore the plain prompt and tell the
                # UI so it shows "general knowledge" rather than stale
                # citations from the previous turn.
                logger.info(f"[RAG] Skipped (conversational): '{raw_text}' — saved ~2s")
                messages[0] = {"role": "system", "content": self._base_prompt}
                await self._publish_sources([])
                await self.push_frame(frame, direction)
                return

            t_start = time.perf_counter()
            try:
                search_query, retrieved, sources, t_rewrite, t_search = (
                    await asyncio.wait_for(
                        self._retrieve(raw_text), timeout=RETRIEVAL_BUDGET_SECONDS
                    )
                )
            except TimeoutError:
                # Over budget. Let the turn through uncited rather than let
                # the aggregator time out and apologise for mishearing.
                logger.warning(
                    f"[RAG] Retrieval exceeded {RETRIEVAL_BUDGET_SECONDS}s budget for "
                    f"'{raw_text}' — answering without document context so the "
                    f"turn is not discarded"
                )
                messages[0] = {"role": "system", "content": self._base_prompt}
                await self._publish_sources([])
                await self.push_frame(frame, direction)
                return

            # Logged every turn on purpose. Retrieval is the largest single
            # cost in the reply path, and it is the one that grows quietly as
            # documents are added — a number in the log is what makes that
            # visible before a user notices it.
            logger.info(
                f"[RAG] timing: rewrite={t_rewrite:.2f}s search={t_search:.2f}s "
                f"total={time.perf_counter() - t_start:.2f}s"
            )
            await self._publish_sources(sources)
            if retrieved:
                logger.info(f"[RAG] Retrieved {len(retrieved)} chars of context")
                enriched = (
                    f"{self._base_prompt}\n\n"
                    f"IMPORTANT: You have access to the following content extracted from "
                    f"the user's uploaded documents. "
                    f"Use this content to answer their question directly. "
                    f"Do NOT say you cannot access files — the text below IS the document content:\n\n"
                    f"{retrieved}"
                )
                messages[0] = {"role": "system", "content": enriched}
            else:
                logger.warning(f"[RAG] No context found for: '{search_query}'")
                messages[0] = {"role": "system", "content": self._base_prompt}

        await self.push_frame(frame, direction)
