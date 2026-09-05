"""Task 4.6 — watching the external providers, and switching away from a
broken one.

Three services sit between a caller speaking and hearing a reply: Deepgram
for speech recognition, Groq (or OpenAI) for the reply itself, Cartesia for
the voice. All three are somebody else's servers reached over the internet,
and when one of them degrades the caller hears nothing at all — no error
message, because there is nowhere to put one in a phone call.

Two halves to this file:

  1. `ProviderHealthObserver` notices when a provider fails or works, and
     tells the breaker. It is an observer rather than a processor in the
     pipeline because an observer sees every frame in both directions from
     one place, including the error frames a service pushes UPSTREAM — which
     a processor sitting after that service would never see at all.

  2. `fallback_for()` is read by the provider factory when a call starts. If
     a provider's breaker is open, the call is built with the local backup
     instead of waiting to discover the outage for itself.

The honest limit, stated plainly because it changes what this task delivers:
**the switch happens at the start of a call, not in the middle of one.**
Pipecat builds a pipeline once and runs it; swapping a live Cartesia
websocket for an in-process Piper halfway through a sentence is not
something the framework supports, and pretending otherwise would be worse
than saying so. So the sequence during a real outage is: the first call or
two hit it and recover through the existing per-service retries, the
breaker trips, and every call after that is built on the backup from the
first word. That is "within seconds" in the sense that matters — the fix
reaches the queue of waiting callers, not the one unlucky person already
mid-sentence.
"""

from __future__ import annotations

from loguru import logger
from pipecat.frames.frames import (
    ErrorFrame,
    LLMFullResponseEndFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

from app.core import breaker
from app.core.config import settings

# Breaker names. Namespaced so they never collide with the per-host tool
# breakers (`tool:<host>`) in the same store.
STT_DEEPGRAM = "provider:stt:deepgram"
TTS_CARTESIA = "provider:tts:cartesia"
LLM_GROQ = "provider:llm:groq"
LLM_OPENAI = "provider:llm:openai"

# Cloud provider -> the backup used when its breaker is open.
#
# Both backups are LOCAL, and that is the point: a cloud outage is not
# usefully answered by a different cloud service that may be having the same
# bad afternoon, and the local path (task 2.2) is already installed, already
# supports this project's two languages, and costs nothing per call. It is
# slower. A slower call is not in the same category of problem as a silent
# one.
FALLBACKS = {
    STT_DEEPGRAM: "whisper",
    TTS_CARTESIA: "piper",
    LLM_GROQ: "openai",
}

# Tighter than the tool defaults. A provider is shared by every live call at
# once, so its failures arrive in a burst rather than one at a time, and the
# cost of trying a broken one is a caller hearing nothing.
PROVIDER_BREAKER = breaker.BreakerConfig(
    failure_threshold=3,
    window_seconds=45.0,
    cooldown_seconds=30.0,
)

for _name in (STT_DEEPGRAM, TTS_CARTESIA, LLM_GROQ, LLM_OPENAI):
    breaker.configure(_name, PROVIDER_BREAKER)


# The class name pipecat gives each service, mapped to the breaker it
# reports to. Matched on the class name rather than by importing the classes
# because the local services (Whisper, Piper) are deliberately imported lazily
# — see the note at the top of providers.py — and importing them here to do
# an isinstance check would undo that on every call.
_BY_CLASS = {
    "DeepgramSTTService": STT_DEEPGRAM,
    "CartesiaTTSService": TTS_CARTESIA,
    "ResilientCartesiaTTSService": TTS_CARTESIA,
    "GroqLLMService": LLM_GROQ,
    "OpenAILLMService": LLM_OPENAI,
}

# What each provider producing its normal output looks like. Seeing one of
# these is proof the provider is working, which is what closes a breaker
# after an outage ends.
_SUCCESS_FRAMES = {
    STT_DEEPGRAM: TranscriptionFrame,
    TTS_CARTESIA: TTSAudioRawFrame,
    LLM_GROQ: LLMFullResponseEndFrame,
    LLM_OPENAI: LLMFullResponseEndFrame,
}


def _breaker_for(processor) -> str | None:
    if processor is None:
        return None
    return _BY_CLASS.get(type(processor).__name__)


class ProviderHealthObserver(BaseObserver):
    """Turns what the pipeline is already doing into breaker signals.

    Deliberately does nothing but observe. It never blocks a frame, never
    changes one, and never raises — an exception in here would take down a
    call over a monitoring concern, which would be a worse bug than anything
    it is watching for.
    """

    def __init__(self) -> None:
        super().__init__()
        # One success per provider per call is enough to close a breaker.
        # Without this guard, a normal call reports success on every audio
        # chunk Cartesia produces — thousands of pointless writes.
        self._reported: set[str] = set()

    async def on_push_frame(self, data: FramePushed) -> None:
        try:
            frame = data.frame

            if isinstance(frame, ErrorFrame):
                # ErrorFrame carries the processor that raised it, which is
                # the only reliable way to tell whose outage this is: by the
                # time the frame reaches anywhere useful, the text alone
                # ("Unknown error occurred") says nothing about the source.
                name = _breaker_for(frame.processor) or _breaker_for(data.source)
                if name:
                    breaker.record_failure(name, frame.error[:200])
                    self._reported.discard(name)
                return

            name = _breaker_for(data.source)
            if name is None or name in self._reported:
                return
            if isinstance(frame, _SUCCESS_FRAMES[name]):
                self._reported.add(name)
                breaker.record_success(name)
        except Exception as e:  # never let monitoring break a call
            logger.debug(f"[BREAKER] provider observer ignored an error: {e}")


def fallback_for(name: str) -> str | None:
    """The provider to use instead, or None to carry on as configured.

    Returns None — carry on — in three cases, each for its own reason:

      - the breaker is closed. Nothing is wrong.
      - fallback is switched off in settings. An operator's call.
      - the backup is not actually usable on this machine. Falling back to a
        model that has to be downloaded first would turn a slow call into a
        call that never starts, which is the opposite of the point. See
        `backup_is_ready()`.
    """
    if not settings.provider_fallback_enabled:
        return None
    if breaker.allows(name):
        return None

    backup = FALLBACKS.get(name)
    if backup is None:
        return None

    ready, why = backup_is_ready(backup)
    if not ready:
        logger.error(
            f"[BREAKER] {name} is open, but its backup ({backup}) is not usable: {why}. "
            f"Carrying on with the failing provider — see scripts/prefetch_local_models.py"
        )
        return None

    logger.warning(f"[BREAKER] {name} is open — this call will use {backup} instead")
    return backup


def backup_is_ready(backup: str) -> tuple[bool, str]:
    """Can this machine actually run the backup right now?

    Checked at call start, and cheaply — an import and a directory listing,
    no model loading. The expensive part (loading weights) happens inside
    the service itself, and by then it is too late to change our mind.
    """
    if backup == "openai":
        if not settings.openai_api_key:
            return False, "OPENAI_API_KEY is not configured"
        return True, ""

    if backup == "whisper":
        # faster-whisper resolves the model through huggingface_hub, which
        # will happily spend a minute downloading it mid-call if it is not
        # already cached. Ask the hub whether it is, without fetching.
        try:
            from huggingface_hub import try_to_load_from_cache
        except ImportError:  # pragma: no cover - huggingface_hub is a hard dep
            return False, "huggingface_hub is not installed"
        repo = f"Systran/faster-whisper-{settings.whisper_model}"
        hit = try_to_load_from_cache(repo, "model.bin")
        if isinstance(hit, str):
            return True, ""
        return False, f"{repo} is not in the local model cache"

    if backup == "piper":
        from app.pipeline.providers import LOCAL_MODELS_DIR

        voices = list(LOCAL_MODELS_DIR.glob("*.onnx"))
        if voices:
            return True, ""
        return False, f"no Piper voice (.onnx) found in {LOCAL_MODELS_DIR}"

    return False, f"unknown backup '{backup}'"


def health() -> dict:
    """What the health endpoint and the metrics export report about
    providers: which are tripped, and whether their backup would actually
    work if they tripped right now."""
    out = {}
    for name, backup in FALLBACKS.items():
        ready, why = backup_is_ready(backup)
        out[name] = {
            "state": breaker.state(name),
            "backup": backup,
            "backup_ready": ready,
            "backup_problem": why or None,
        }
    return out
