from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from app.core.config import settings


async def run_voice_pipeline(
    webrtc_connection: SmallWebRTCConnection,
    bot_name: str,
    system_prompt: str,
    voice_id: str,
    llm_model: str,
    language: str = "en",
):
    logger.info(f"[PIPELINE] Starting for bot: {bot_name}")

    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(params=VADParams(
                confidence=0.4,
                min_volume=0.2,
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

    pipeline = Pipeline([
        transport.input(),
        stt,
        context_aggregator.user(),
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])

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
