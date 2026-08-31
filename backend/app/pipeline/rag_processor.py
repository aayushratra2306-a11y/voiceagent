from loguru import logger
from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.processors.aggregators.llm_context import LLMContext

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

    def __init__(self, bot_id: str, llm_context: LLMContext, base_system_prompt: str):
        super().__init__()
        self._bot_id = bot_id
        self._context = llm_context
        self._base_prompt = base_system_prompt

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
            retrieved = await query_context(self._bot_id, search_query)
            if retrieved:
                logger.info(f"[RAG] Retrieved {len(retrieved)} chars of context")
                enriched = (
                    f"{self._base_prompt}\n\n"
                    f"IMPORTANT: You have access to the following content extracted from the user's uploaded documents. "
                    f"Use this content to answer their question directly. "
                    f"Do NOT say you cannot access files — the text below IS the document content:\n\n"
                    f"{retrieved}"
                )
                self._context.messages[0] = {"role": "system", "content": enriched}
            else:
                logger.warning(f"[RAG] No context found for: '{search_query}'")
                self._context.messages[0] = {"role": "system", "content": self._base_prompt}

        await self.push_frame(frame, direction)
