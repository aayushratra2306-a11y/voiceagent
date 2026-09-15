"""Found 2026-09-14 while verifying Task 4.6 on the server.

The local backup models had never been downloaded (local_models/ was empty),
so during a real Cartesia outage that morning the TTS breaker tripped exactly
as designed and the fallback it reached for could not run. Running
scripts/prefetch_local_models.py fixed that, and showed a second gap:

  - Piper voices went to /app/local_models, a named volume: 121 MB, kept.
  - The Whisper model went to huggingface's default cache,
    /root/.cache/huggingface: 464 MB inside the container's own filesystem,
    wiped by the next `up -d --build`, which every deploy runs.

Nothing would have said so. backup_is_ready() checks at call time, so the
speech-recognition fallback would quietly go back to "not ready" after the
next deploy and stay that way until the next outage exposed it.

The fix points HF_HOME into the local_models volume, which is where
providers.py's own comment already said Whisper weights were meant to live.
No real download or network request is made in this file.
"""

import os
import subprocess
import sys
from pathlib import Path

import yaml

BACKEND = Path(__file__).resolve().parents[1]
COMPOSE = BACKEND.parent / "deploy" / "docker-compose.yml"
DOCKERFILE = BACKEND.parent / "deploy" / "Dockerfile"


def _backend_service() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["backend"]


def _named_volume_targets() -> dict[str, str]:
    """mount target -> volume name, for named volumes only (not bind mounts)."""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    declared = set((compose.get("volumes") or {}).keys())
    targets = {}
    for entry in _backend_service().get("volumes", []):
        source, _, target = str(entry).partition(":")
        target = target.split(":")[0]
        if source in declared:
            targets[target] = source
    return targets


def _hf_home() -> str | None:
    env = _backend_service().get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return env.get("HF_HOME")


def test_the_whisper_backup_is_cached_inside_a_persistent_volume():
    hf_home = _hf_home()
    assert hf_home, (
        "HF_HOME is not set for the backend: the Whisper backup lands in the container's own "
        "filesystem and every rebuild deletes it"
    )
    inside = [target for target in _named_volume_targets() if hf_home.rstrip("/").startswith(target.rstrip("/") + "/")]
    assert inside, f"HF_HOME={hf_home} is not inside any named volume mounted on the backend"


def _local_models_in_image() -> str:
    """LOCAL_MODELS_DIR is backend/local_models, which the image copies into WORKDIR."""
    workdir = next(
        line.split(None, 1)[1].strip()
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if line.startswith("WORKDIR ")
    )
    return f"{workdir.rstrip('/')}/local_models"


def test_that_volume_is_the_one_the_piper_backup_already_uses():
    """One place for every local backup model, where the code looks for them."""
    local_models = _local_models_in_image()

    assert local_models in _named_volume_targets(), "local_models is no longer a named volume"
    assert _hf_home().startswith(local_models + "/")


def test_the_cache_does_not_sit_where_piper_looks_for_voices():
    """Piper readiness globs *.onnx directly in local_models. The Whisper cache
    has to be a subdirectory, never the directory itself."""
    assert _hf_home().rstrip("/") != _local_models_in_image()


def _check_whisper_with_hf_home(hf_home: Path, *, cached: bool) -> str:
    """Runs the real readiness check in a fresh interpreter: huggingface_hub
    reads HF_HOME once, at import, so it cannot be changed in this process."""
    script = f"""
from pathlib import Path
from app.core.config import settings
if {cached!r}:
    repo = Path(r"{hf_home}") / "hub" / f"models--Systran--faster-whisper-{{settings.whisper_model}}"
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / "main").write_text("0123456789abcdef")
    snapshot = repo / "snapshots" / "0123456789abcdef"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"x")
from app.pipeline.provider_health import _check_backup
print(_check_backup("whisper")[0])
"""
    env = {**os.environ, "HF_HOME": str(hf_home), "HF_HUB_OFFLINE": "1"}
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout.strip().splitlines()[-1]


def test_the_readiness_check_follows_hf_home(tmp_path):
    """Pins the mechanism the fix relies on: a model cached under HF_HOME is
    what the running server counts as a usable Whisper backup."""
    assert _check_whisper_with_hf_home(tmp_path / "with", cached=True) == "True"
    assert _check_whisper_with_hf_home(tmp_path / "without", cached=False) == "False"
