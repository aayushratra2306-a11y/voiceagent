# Task 2.3 — backend container.
#
# python:3.11-slim rather than alpine: several dependencies here ship
# manylinux wheels but not musl ones (onnxruntime for Smart Turn v3,
# ctranslate2 for faster-whisper), and building those from source on a
# free-tier VM is not a good time. Slim is the right tradeoff.
#
# Works on ARM64 (Oracle's Always Free Ampere shape) as well as x86 — all
# the heavy wheels here publish aarch64 Linux builds.
FROM python:3.11-slim

# ffmpeg: audio decode/resample used across the pipeline.
# build-essential: only needed if a wheel is missing for this arch and pip
# has to build from source; harmless otherwise.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first so this layer caches independently of source
# changes — a code edit shouldn't trigger a full dependency reinstall.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

# Smart Turn v3's ONNX model ships inside the pipecat package, so nothing
# to download at build time. Local Whisper/Piper models (Task 2.2) are NOT
# baked in: production defaults to the cloud providers, and the local
# models are large. If you switch STT_PROVIDER/TTS_PROVIDER to local, the
# first call downloads them into the mounted local_models volume.

EXPOSE 8080

# One uvicorn worker on purpose: Task 2.4 already gives every CALL its own
# process, and the in-memory call registry in app/api/connect.py assumes a
# single API process (a second worker wouldn't find another worker's
# calls when routing ICE candidates). Scale calls, not API workers, until
# that registry moves to Redis.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
