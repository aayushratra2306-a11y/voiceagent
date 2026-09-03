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
from pipecat.services.deepgram.stt import DeepgramSTTService, DeepgramSTTSettings
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.openai.llm import OpenAILLMService
from websockets.protocol import State

from app.core.config import settings

# The LOCAL providers (Whisper, Piper) are imported inside their factory
# branches, not here. Task 2.4 spawns a fresh interpreter per call, so every
# module imported at this level is re-imported on every single call before
# the caller hears anything. faster-whisper pulls in CTranslate2 and costs
# about 2s of that startup — paid on every call even when stt_provider is
# 'deepgram', which is the default and what the deployed server actually
# runs. Measured 2026-09-03: 3.75s to import with it, 1.71s without.


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
        # Imported here, not at module level — see the note by the imports.
        from pipecat.services.whisper.stt import WhisperSTTService

        logger.info(f"[PROVIDERS] STT: local Whisper ({settings.whisper_model}, cpu)")
        return WhisperSTTService(
            model=settings.whisper_model,
            device="cpu",
        )

    # endpointing/interim_results explicit rather than left at Deepgram's
    # undocumented default — see voice_pipeline.py's original Task 1.1 note
    # for the full story (a final transcript never arrived without this).
    #
    # endpointing=500, not 300 (found 2026-09-03 from live transcripts).
    # This is the silence duration, in ms, before Deepgram closes a chunk
    # out as `is_final` and pipecat pushes it downstream as its own
    # TranscriptionFrame. Pipecat 1.7.0's Deepgram wrapper does this
    # unconditionally for every is_final result — confirmed by reading
    # _on_message directly — with no merging and no handler for Deepgram's
    # own UtteranceEnd event (utterance_end_ms is accepted and forwarded,
    # but nothing consumes the event it produces in this version). So a
    # pause anywhere above the endpointing threshold splits one spoken
    # sentence into multiple separate "final" transcripts — confirmed live:
    # "this today's date and time, I mean," and "I just" arrived as two
    # unrelated user turns for what was one continuous sentence with a
    # mid-thought pause.
    #
    # 300ms was more aggressive than the turn-detector already tolerates:
    # the VAD's own stop_secs is 500ms (raised deliberately on 2026-08-31,
    # see the long note in voice_pipeline.py, after live testing showed the
    # bot cutting people off before that change). Deepgram closing a chunk
    # at 300ms while the rest of the system already accepts a pause up to
    # 500ms as still-mid-turn meant Deepgram was fragmenting sentences the
    # turn-detector itself would have waited through. Matching the two
    # numbers doesn't eliminate the risk — a pause longer than 500ms still
    # splits — but it stops Deepgram being the MORE trigger-happy of the
    # two systems making this decision.
    #
    # (What this alone didn't fix — RAGContextProcessor reacting to every
    # fragment instead of a real completed turn — was addressed separately
    # in rag_processor.py's latest_user_text(), 2026-09-03.)
    #
    # ttfs_p99_latency=0.7 (found 2026-09-03, following pipecat's own
    # documented remedy, not a guess). Deepgram's built-in default here is
    # 0.35s — but pipecat's stt_latency.py states plainly that figure was
    # benchmarked at their recommended stop_secs=0.2, and says explicitly:
    # "If you change stop_secs, re-run the benchmark ... and pass the
    # measured value to your STT service constructor." We changed stop_secs
    # to 0.5 (see the note above) and never did that — the result, logged on
    # every single call: "STT wait timeout collapsed to 0s, which may cause
    # delayed turn detection". Confirmed by reading
    # turn_analyzer_user_turn_stop_strategy.py directly: once
    # stt_timeout <= stop_secs, the safety-net window that's supposed to
    # wait a little extra for a transcript to actually arrive computes to
    # zero and stops doing its job.
    #
    # 0.7 is a deliberately conservative margin above 0.5, not a re-run
    # benchmark result — this environment has no way to run pipecat's own
    # https://github.com/pipecat-ai/stt-benchmark tool against this specific
    # network path. It's large enough to guarantee the collapse condition
    # never triggers, small enough to add at most ~200ms over the minimum
    # that would satisfy it. Re-running the real benchmark would give a
    # more precise number; this removes the defect without needing to.
    #
    # This is NOT a fix for Smart Turn's own COMPLETE/INCOMPLETE judgment —
    # checked directly: BaseSmartTurn and LocalSmartTurnV3 expose no
    # confidence threshold or similar tunable, only stop_secs (already set
    # above). Smart Turn misjudging a genuine pause is a separate, real
    # model-accuracy limit this change does not touch.
    # BUG FOUND 2026-09-03, and it's a serious one: language, endpointing and
    # interim_results were being passed as bare keyword arguments to
    # DeepgramSTTService(...) — which silently drops every one of them. This
    # is the SAME silent-extra-field-drop pattern already found twice before
    # in this project (Task 1.1's VAD params, and PipelineParams' audio
    # fields) — a fourth occurrence would have been worth a project-wide
    # sweep on its own, but three is already the pattern to watch for.
    #
    # Confirmed directly by constructing the real service and inspecting
    # `_settings` afterward: a bare `language="hi"` produced `_settings.
    # language == "en"` — the class's own hardcoded default, completely
    # unaffected by what we passed. Concretely, this means every Hindi-
    # configured bot has been transcribed as if it were English on this,
    # the DEFAULT cloud path, for as long as this code has existed. The
    # earlier "faster-whisper verified live in English and Hindi" note in
    # project memory is real but describes the LOCAL provider path
    # (stt_provider="whisper") — a different code path from this one, which
    # is what every bot actually uses unless explicitly switched over.
    # `endpointing=500` (this session's earlier fix, commit 55059c7) never
    # took effect either, for the same reason — Deepgram has been running
    # on its own undocumented server-side default the whole time regardless
    # of what number was written here.
    #
    # The Deepgram service's docstring names the fix directly: these three
    # are "runtime-updatable fields" that belong on `settings=Deepgram
    # STTService.Settings(...)`, not passed as init kwargs — init kwargs are
    # for "connection-level config" only (api_key, ttfs_p99_latency, and the
    # like). Verified the corrected form actually lands: constructing with
    # `settings=DeepgramSTTSettings(language="hi", ...)` produces
    # `_settings.language == "hi"`, matching what was intended all along.
    logger.info("[PROVIDERS] STT: Deepgram (cloud)")
    return DeepgramSTTService(
        api_key=settings.deepgram_api_key,
        ttfs_p99_latency=0.7,
        settings=DeepgramSTTSettings(
            language=language,
            endpointing=500,
            interim_results=True,
        ),
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
        # Imported here, not at module level — see the note by the imports.
        from pipecat.services.piper.tts import PiperTTSService

        voice = _PIPER_VOICES.get(_base_lang(language), _PIPER_VOICES["en"])
        logger.info(f"[PROVIDERS] TTS: local Piper (voice={voice})")
        return PiperTTSService(
            voice_id=voice,
            download_dir=LOCAL_MODELS_DIR,
        )

    logger.info("[PROVIDERS] TTS: Cartesia (cloud)")
    return ResilientCartesiaTTSService(api_key=settings.cartesia_api_key, voice_id=voice_id, language=language)
