from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import Frame, TranscriptionFrame, TTSSpeakFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from app.core.config import settings
from app.pipeline.rag_processor import RAGContextProcessor


class AudioDebugger(FrameProcessor):
    """Logs every frame so we can see what types flow through the pipeline."""
    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        name = type(frame).__name__
        if isinstance(frame, UserStartedSpeakingFrame):
            logger.info("[AUDIO] VAD: user started speaking")
        elif isinstance(frame, UserStoppedSpeakingFrame):
            logger.info("[AUDIO] VAD: user stopped speaking")
        elif isinstance(frame, TranscriptionFrame):
            logger.info(f"[AUDIO] TranscriptionFrame: '{frame.text}'")
        elif any(k in name for k in ("Transcri", "STT", "Speech", "Word", "Text")):
            logger.info(f"[AUDIO] {name}: text={getattr(frame, 'text', getattr(frame, 'content', '?'))!r}")
        await self.push_frame(frame, direction)


async def run_voice_pipeline(
    webrtc_connection: SmallWebRTCConnection,
    bot_name: str,
    system_prompt: str,
    voice_id: str,
    llm_model: str,
    language: str = "en",
    bot_id: str | None = None,
):
    logger.info(f"[PIPELINE] Starting for bot: {bot_name}")

    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(params=VADParams(
                confidence=0.6,
                min_volume=0.25,
                start_secs=0.2,
                stop_secs=0.8,
            )),
        ),
    )

    stt = DeepgramSTTService(api_key=settings.deepgram_api_key, language=language)
    llm = OpenAILLMService(api_key=settings.openai_api_key, model=llm_model)
    tts = CartesiaTTSService(api_key=settings.cartesia_api_key, voice_id=voice_id, language=language)

    context = LLMContext(messages=[{"role": "system", "content": system_prompt}])
    context_aggregator = LLMContextAggregatorPair(context)

    pipeline_steps = [transport.input(), stt, AudioDebugger()]

    if bot_id:
        pipeline_steps.append(RAGContextProcessor(bot_id, context, system_prompt))

    pipeline_steps += [
        context_aggregator.user(),
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ]

    pipeline = Pipeline(pipeline_steps)

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("[PIPELINE] Client connected — sending greeting")
        await task.queue_frame(TTSSpeakFrame("Hello! I'm ready. How can I help you?"))

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("[PIPELINE] Client disconnected")
        await task.cancel()

    logger.info("[PIPELINE] Running…")
    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
