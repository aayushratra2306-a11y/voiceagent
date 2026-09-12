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

# Found 2026-09-12, while trying to read a live call's tool calls back out
# of `docker compose logs`: 213 [PIPELINE] lines in the window and not one
# [TOOL] line, for any tool, on calls that demonstrably made tool calls.
#
# Python block-buffers stdout when it is a pipe rather than a terminal —
# which is exactly what it is inside a container. Lines sit in that buffer
# until it fills or the process exits CLEANLY. Task 2.4 gives every call its
# own worker process and ends it with Process.terminate() (SIGTERM), so a
# call's last few log lines were not merely delayed, they were discarded
# with the buffer. The lines that did survive were the ones written early
# enough in a long call to be pushed out by a later buffer flush.
#
# This is a debugging-blindness bug rather than a runtime one: nothing broke
# for a caller, but the evidence needed to diagnose what did was being
# thrown away — which is how the datetime defect above ended up having to be
# diagnosed from what the user heard instead of from the logs.
ENV PYTHONUNBUFFERED=1

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

# Task 4.7 — orchestrator-level detection, complementing the in-process
# watchdog (app/core/health.py) rather than duplicating it: that watchdog
# restarts on a broken DEPENDENCY (the database unreachable) even while the
# event loop is otherwise responsive; this catches the process not being
# able to answer AT ALL — wedged, or dead — from outside it entirely, which
# a check running on that same possibly-wedged loop cannot.
#
# python -c ... rather than curl/wget: keeps the image free of an extra
# apt package for a single GET request. --start-period gives the pipeline
# stack (Silero VAD, Smart Turn's ONNX model) time to import on the first
# boot before failed checks start counting.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request as u; import sys; sys.exit(0 if u.urlopen('http://localhost:8080/health', timeout=4).status == 200 else 1)"

# One uvicorn worker on purpose: Task 2.4 already gives every CALL its own
# process, and the in-memory call registry in app/api/connect.py assumes a
# single API process (a second worker wouldn't find another worker's
# calls when routing ICE candidates). Scale calls, not API workers, until
# that registry moves to Redis.
#
# --forwarded-allow-ips="*" is load-bearing for rate limiting, not a
# convenience. Task 2.5's key function falls back to the client IP for
# every unauthenticated request (/auth/login, /refresh, /register), and
# slowapi reads that from request.client.host. Without this flag uvicorn
# only rewrites request.client from X-Forwarded-For when the immediate
# peer is 127.0.0.1 — but the only thing that ever connects here is Caddy,
# from its container address on the compose bridge, which is not
# loopback. So every unauthenticated caller collapsed to Caddy's one IP
# and shared a single global rate-limit bucket: five failed logins a
# minute from anyone locked out login for everyone. "*" is safe
# specifically because port 8080 is EXPOSE-only, never published to the
# host (see docker-compose.yml) — Caddy is the only possible client, and
# Caddy is trusted to set X-Forwarded-For correctly.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--forwarded-allow-ips", "*"]
