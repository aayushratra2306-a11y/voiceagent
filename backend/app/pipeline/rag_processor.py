from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.processors.aggregators.llm_context import LLMContext

from app.services.rag import query_context


class RAGContextProcessor(FrameProcessor):
    """
    Sits between STT and the LLM context aggregator.
    On every transcription, queries Pinecone and injects
    relevant document context into the system prompt.
    """

    def __init__(self, bot_id: str, llm_context: LLMContext, base_system_prompt: str):
        super().__init__()
        self._bot_id = bot_id
        self._context = llm_context
        self._base_prompt = base_system_prompt

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            retrieved = await query_context(self._bot_id, frame.text)
            if retrieved:
                enriched = (
                    f"{self._base_prompt}\n\n"
                    f"Relevant information from uploaded documents:\n{retrieved}\n\n"
                    f"Use this information to answer if it is relevant to what the user asked."
                )
                self._context.messages[0] = {"role": "system", "content": enriched}
            else:
                self._context.messages[0] = {"role": "system", "content": self._base_prompt}

        await self.push_frame(frame, direction)
