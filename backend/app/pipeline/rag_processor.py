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

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)

        # Only run RAG on final transcriptions — interim frames are partial
        # and produce low-quality queries that pollute the context
        is_final = isinstance(frame, TranscriptionFrame)
        if not is_final and InterimTranscriptionFrame:
            is_final = False  # explicitly skip interim

        if is_final and hasattr(frame, 'text') and frame.text and frame.text.strip():
            raw_text = frame.text
            search_query = await rewrite_query(raw_text)
            # Keep the raw transcript in the log even though search_query is
            # what actually gets used — per task 1.6, useful for spotting a
            # rewrite that went wrong without needing to reproduce it live.
            if search_query != raw_text:
                logger.info(f"[RAG] Query rewritten: '{raw_text}' -> '{search_query}'")
            else:
                logger.info(f"[RAG] Query (unchanged): '{raw_text}'")
            retrieved, sources = await query_context(self._bot_id, search_query)
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
