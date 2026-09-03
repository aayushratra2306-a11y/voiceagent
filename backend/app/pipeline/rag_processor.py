import asyncio
import re
import time

from loguru import logger
from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameProcessor

from app.models.document import Document
from app.services.rag import query_context, rewrite_query

try:
    from pipecat.frames.frames import InterimTranscriptionFrame
except ImportError:
    InterimTranscriptionFrame = None


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

# How long retrieval may hold a transcript frame before we give up and let it
# through uncited.
#
# BUG FOUND 2026-09-03 from live logs. This processor sits BEFORE the user
# aggregator in the pipeline and does not push the frame until retrieval
# finishes. The aggregator has its own `user_turn_stop_timeout`, 5.0s by
# default, after which it decides no transcript is coming and fires
# on_user_turn_stop_timeout — which makes the bot say "Sorry, I didn't catch
# that". So a lookup slower than 5s produced exactly the wrong behaviour: the
# bot had heard the caller perfectly and was still searching, but apologised
# for mishearing and threw the turn away. Measured instance: retrieval took
# 7.03s, the aggregator gave up at 4.1s, the frame arrived 2.9s after that.
#
# 3.5s leaves headroom under the aggregator's 5.0s. Raising one without the
# other reintroduces the bug, which is why both numbers are named here.
#
# Exceeding the budget degrades to answering from general knowledge — a
# slightly worse answer. Blowing the aggregator timeout discards the turn
# entirely and blames the caller. The first is plainly the better failure.
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


class RAGContextProcessor(FrameProcessor):
    """
    Sits between STT and the LLM context aggregator.
    Only fires on FINAL TranscriptionFrames (not interim partials)
    so the query is complete before Pinecone is called.
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

        # Only run RAG on final transcriptions — interim frames are partial
        # and produce low-quality queries that pollute the context
        is_final = isinstance(frame, TranscriptionFrame)
        if not is_final and InterimTranscriptionFrame:
            is_final = False  # explicitly skip interim

        if is_final and hasattr(frame, 'text') and frame.text and frame.text.strip():
            raw_text = frame.text

            if not needs_retrieval(raw_text):
                # Nothing to look up. Restore the plain prompt and tell the
                # UI so it shows "general knowledge" rather than stale
                # citations from the previous turn.
                logger.info(f"[RAG] Skipped (conversational): '{raw_text}' — saved ~2s")
                self._context.messages[0] = {"role": "system", "content": self._base_prompt}
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
                self._context.messages[0] = {"role": "system", "content": self._base_prompt}
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
                self._context.messages[0] = {"role": "system", "content": enriched}
            else:
                logger.warning(f"[RAG] No context found for: '{search_query}'")
                self._context.messages[0] = {"role": "system", "content": self._base_prompt}

        await self.push_frame(frame, direction)
