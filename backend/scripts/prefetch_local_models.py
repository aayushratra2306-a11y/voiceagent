"""Task 4.6 — download the backup providers before they are needed.

Run this ONCE per machine, ahead of time:

    python -m scripts.prefetch_local_models

Why it has to be ahead of time. When Deepgram or Cartesia trips its circuit
breaker, the next call is built on the local backup instead. If that
backup's model files are not already on disk, the "fallback" is really a
several-hundred-megabyte download starting while somebody is on the phone
waiting to be greeted — which is a worse outcome than the outage it was
supposed to soften. So the fallback checks first and refuses to switch to a
backup that is not present (see provider_health.backup_is_ready). This
script is what makes it present.

Disk cost, on the deployed VM's 30GB:
    faster-whisper 'small'   ~ 500 MB
    Piper voices (en + hi)   ~  120 MB

Nothing here needs an API key, costs money, or contacts any paid service —
both come from public model hosting.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.pipeline.providers import _PIPER_VOICES, LOCAL_MODELS_DIR  # noqa: E402


def fetch_whisper() -> bool:
    """faster-whisper resolves its model through huggingface_hub's cache, so
    constructing the model once is what puts it there."""
    repo = f"Systran/faster-whisper-{settings.whisper_model}"
    logger.info(f"Fetching speech recognition backup: {repo}")
    try:
        from faster_whisper import WhisperModel

        WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")
    except Exception as e:
        logger.error(f"  failed: {type(e).__name__}: {e}")
        return False
    logger.info("  ready")
    return True


def fetch_piper() -> bool:
    """Piper downloads a voice into download_dir on first construction. Both
    languages this project actually supports are fetched — a Hindi caller
    reaching an English-only backup would be no fallback at all."""
    ok = True
    LOCAL_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for language, voice in _PIPER_VOICES.items():
        logger.info(f"Fetching speech synthesis backup ({language}): {voice}")
        try:
            from pipecat.services.piper.tts import PiperTTSService

            PiperTTSService(voice_id=voice, download_dir=LOCAL_MODELS_DIR)
        except Exception as e:
            logger.error(f"  failed: {type(e).__name__}: {e}")
            ok = False
            continue
        logger.info("  ready")
    return ok


def main() -> int:
    from app.pipeline.provider_health import backup_is_ready

    results = {"whisper": fetch_whisper(), "piper": fetch_piper()}

    logger.info("")
    logger.info("Checking the way the running server checks:")
    exit_code = 0
    for backup in ("whisper", "piper", "openai"):
        ready, why = backup_is_ready(backup)
        logger.info(f"  {backup:<8} {'ready' if ready else 'NOT READY — ' + why}")
        if not ready and backup in results:
            exit_code = 1

    if exit_code == 0:
        logger.info("")
        logger.info("Both local backups are usable. A tripped provider breaker will now "
                    "switch to them instead of carrying on with the failing provider.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
