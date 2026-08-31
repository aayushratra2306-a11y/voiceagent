"""Task 2.1 — the provider switching layer.

Every "which service do we actually use" decision lives here, behind three
factory functions. voice_pipeline.py calls these instead of constructing
Deepgram/Cartesia/Groq/OpenAI services directly, so swapping a provider is a
one-line settings change (app/core/config.py's stt_provider/tts_provider/
llm_provider) — no pipeline code touched.

Deliberately thin, per the manual's own tip: pipecat's STT/TTS/LLM services
already share a common base-class interface (STTService, TTSService,
LLMService), so this is mostly plumbing, not a new abstraction. Adding
provider-specific special cases into the CALLERS of these factories (rather
than inside the factories, where they belong) is exactly how this kind of
layer quietly stops working — resist that.
"""

import asyncio
from pathlib import Path

from loguru import logger
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.piper.tts import PiperTTSService
from pipecat.services.whisper.stt import WhisperSTTService
from websockets.protocol import State

from app.core.config import settings

# Task 2.2 — where local model files (Whisper weights via faster-whisper's
# own cache, Piper .onnx voices) get stored. Keeping this inside the repo
# (gitignored) rather than the OS default cache dir makes "where did my
# disk space go" and "move this to another machine" both obvious.
LOCAL_MODELS_DIR = Path(__file__).resolve().parent.parent.parent / "local_models"
LOCAL_MODELS_DIR.mkdir(exist_ok=True)


class ResilientCartesiaTTSService(CartesiaTTSService):
    """CartesiaTTSService.start() calls _connect_websocket() exactly once,
    with no retry — confirmed by reading pipecat's source directly. Pipecat's
    own WebsocketService reconnect-on-error logic only covers a connection
    that drops AFTER being established; it does not cover this first attempt.

    In practice, this project's network path to Cartesia has been
    intermittently slow enough (hit repeatedly on 2026-08-30, 4 separate
    times in one session — confirmed via direct connection timing tests
    ranging 0.15s to 6+s) to exceed the handshake timeout on a single try.
    Since the greeting is the very first thing every session does, one slow
    attempt meant total silence with no recovery — this is what fixes that.

    Retries only the initial connection a few times with a short backoff.
    Does not touch pipecat's own mid-session reconnect behaviour at all.

    Moved here from voice_pipeline.py in Task 2.1 — this is provider-specific
    plumbing, so it belongs with the rest of the provider layer, not in the
    pipeline construction code that now just calls get_tts_service().
    """

    async def _connect_websocket(self):
        if self._websocket and self._websocket.state is State.OPEN:
            return

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.debug(f"Connecting to Cartesia TTS (attempt {attempt}/{max_attempts})")
                self._websocket = await self._websocket_connect(
                    f"{self._url}?api_key={self._api_key}&cartesia_version={self._cartesia_version}"
                )
                await self._call_event_handler("on_connected")
                return
            except Exception as e:
                self._websocket = None
                if attempt == max_attempts:
                    logger.warning(f"Cartesia TTS: all {max_attempts} connection attempts failed: {e}")
                    await self.push_error(error_msg=f"Unknown error occurred: {e}", exception=e)
                    await self._call_event_handler("on_connection_error", f"{e}")
                else:
                    logger.warning(
                        f"Cartesia TTS connect attempt {attempt}/{max_attempts} failed "
                        f"({e}), retrying…"
                    )
                    await asyncio.sleep(0.5 * attempt)


# Task 2.2 — Piper voices. Picked specifically for this bot's two confirmed
# working languages (English + Hindi, both verified live earlier this
# session with the cloud providers) rather than an English-only default:
# Kyutai's Pocket TTS (the manual's other suggested option) only ships
# DE/EN/ES/FR/IT/PT voices — no Hindi — so it was ruled out for this
# project's actual requirements, even though it's lighter/faster.
_PIPER_VOICES = {
    "hi": "hi_IN-priyamvada-medium",
    "en": "en_US-amy-medium",
}


def _base_lang(language: str | None) -> str:
    return (language or "en").split("-")[0].lower()


def get_stt_service(language: str = "en"):
    """Task 2.1/2.2 — speech-to-text factory. settings.stt_provider:
    'deepgram' (cloud, default) or 'whisper' (local, free, via faster-whisper).
    """
    if settings.stt_provider == "whisper":
        logger.info(f"[PROVIDERS] STT: local Whisper ({settings.whisper_model}, cpu)")
        return WhisperSTTService(
            model=settings.whisper_model,
            device="cpu",
        )

    # endpointing/interim_results explicit rather than left at Deepgram's
    # undocumented default — see voice_pipeline.py's original Task 1.1 note
    # for the full story (a final transcript never arrived without this).
    logger.info("[PROVIDERS] STT: Deepgram (cloud)")
    return DeepgramSTTService(
        api_key=settings.deepgram_api_key,
        language=language,
        endpointing=300,
        interim_results=True,
    )


def get_llm_service(llm_model: str):
    """Task 2.1 — LLM factory. settings.llm_provider: 'auto' (Groq if a key
    is configured, else OpenAI — the exact pre-2.1 behavior, unchanged),
    'groq', or 'openai'. llm_model is the bot's own stored OpenAI model name
    (per-bot config, not a global setting) — only used on the OpenAI path.
    """
    provider = settings.llm_provider
    use_groq = provider == "groq" or (provider == "auto" and bool(settings.groq_api_key))

    if use_groq:
        if not settings.groq_api_key:
            raise ValueError("llm_provider='groq' but GROQ_API_KEY is not configured")
        # reasoning_effort="low": gpt-oss-120b is a reasoning model — without
        # this it can burn its whole token budget "thinking" before writing
        # any reply text (verified directly against Groq's API, Task 1.2).
        logger.info(f"[PROVIDERS] LLM: Groq ({settings.groq_model}, reasoning_effort=low)")
        return GroqLLMService(
            api_key=settings.groq_api_key,
            settings=GroqLLMService.Settings(
                model=settings.groq_model,
                extra={"reasoning_effort": "low"},
            ),
        )

    logger.info(f"[PROVIDERS] LLM: OpenAI ({llm_model})")
    return OpenAILLMService(api_key=settings.openai_api_key, model=llm_model)


def get_tts_service(voice_id: str, language: str = "en"):
    """Task 2.1/2.2 — text-to-speech factory. settings.tts_provider:
    'cartesia' (cloud, default) or 'piper' (local, free, in-process).

    Note on Piper's license: the piper-tts package is GPL-3.0. Running it
    in-process (as here) keeps it out of scope as long as this stays a
    self-hosted service you run rather than proprietary code you redistribute
    — pipecat also ships PiperHttpTTSService (talks to a separately-run
    Piper server over HTTP) specifically for projects that need Piper kept
    fully out of their own codebase's license scope; swap to that if this
    project's distribution plans ever need it.
    """
    if settings.tts_provider == "piper":
        voice = _PIPER_VOICES.get(_base_lang(language), _PIPER_VOICES["en"])
        logger.info(f"[PROVIDERS] TTS: local Piper (voice={voice})")
        return PiperTTSService(
            voice_id=voice,
            download_dir=LOCAL_MODELS_DIR,
        )

    logger.info("[PROVIDERS] TTS: Cartesia (cloud)")
    return ResilientCartesiaTTSService(api_key=settings.cartesia_api_key, voice_id=voice_id, language=language)
