"""Task 2.4 — one OS process per call.

Before this, every live call ran as an asyncio task inside the single
uvicorn process. That meant two real problems, both named directly in the
manual: (1) a crash in any one call's pipeline — a bad frame, an unhandled
exception escaping the pipeline, even a native-library fault in
onnxruntime/ctranslate2 (both in the local Whisper/VAD/Smart-Turn path) —
could take down the whole server and every other live call with it; (2) the
CPU-bound work of several simultaneous calls (VAD, local STT/TTS if
enabled) competed for the same interpreter and the same GIL.

Each call now gets its own `multiprocessing.Process`, spawned per-connect
and torn down when the call ends. A crash in one is contained entirely to
that process; the API server and every other call are unaffected.

Two IPC channels per call, both plain `multiprocessing.Queue`s (chosen over
literal env-vars/a config file — the manual's other suggested options —
because the WebRTC handshake needs the SDP *answer* handed back live, not
just static startup config; a one-shot file can't do that):
  - answer_queue: child -> parent, once, with the SDP answer for the initial
    POST /connect.
  - ice_queue: parent -> child, one item per PATCH /connect/ice call for
    this same pc_id, for as long as the call lasts (trickle ICE).

bot_config itself IS passed as plain picklable data (a dict of strings) via
Process(args=...) — the spirit of "config in, not a shared object across
the process boundary" the manual asks for, just via multiprocessing's own
mechanism rather than a written file.
"""

import asyncio
import multiprocessing as mp

from loguru import logger
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

# An orphaned process (pipeline stuck, cleanup never fired) must not live
# forever quietly eating memory — the manual's own explicit warning about
# this exact failure mode. 1 hour is generously above any real call length.
MAX_CALL_LIFETIME_SECONDS = 60 * 60


def call_worker_main(
    bot_config: dict,
    sdp: str,
    sdp_type: str,
    pc_id: str | None,
    answer_queue: mp.Queue,
    ice_queue: mp.Queue,
) -> None:
    """Process entry point (the `target=` of multiprocessing.Process) — runs
    in the child, start to finish. Never raises back into the parent; any
    failure here is logged and simply ends this one process."""
    try:
        asyncio.run(_worker_main(bot_config, sdp, sdp_type, pc_id, answer_queue, ice_queue))
    except Exception:
        logger.exception(f"[CALL WORKER pid={mp.current_process().pid}] fatal error")


async def _worker_main(
    bot_config: dict,
    sdp: str,
    sdp_type: str,
    pc_id: str | None,
    answer_queue: mp.Queue,
    ice_queue: mp.Queue,
) -> None:
    # Imported here, not at module top level: this module is imported by
    # the PARENT process too (connect.py needs call_worker_main as a spawn
    # target), and voice_pipeline.py pulls in the entire pipecat pipeline
    # stack (Silero VAD, Smart Turn's ONNX model, etc.) — no reason to pay
    # that import cost in the parent, which never runs a pipeline itself.
    from app.pipeline.voice_pipeline import run_voice_pipeline

    handler = SmallWebRTCRequestHandler()
    pipeline_started = asyncio.Event()
    pipeline_task: asyncio.Task | None = None

    async def start_pipeline(webrtc_connection: SmallWebRTCConnection):
        nonlocal pipeline_task

        async def _run():
            try:
                await run_voice_pipeline(webrtc_connection=webrtc_connection, **bot_config)
            except Exception:
                logger.exception(f"[CALL WORKER pid={mp.current_process().pid}] pipeline crashed")

        pipeline_task = asyncio.create_task(_run())
        pipeline_started.set()

    request = SmallWebRTCRequest(sdp=sdp, type=sdp_type, pc_id=pc_id)
    answer = await handler.handle_web_request(request, start_pipeline)
    answer_queue.put({"sdp": answer["sdp"], "type": answer["type"], "pc_id": answer["pc_id"]})

    # start_pipeline is pipecat's own on-connected callback — it fires once
    # the peer connection is actually established, which can be slightly
    # after handle_web_request returns the answer. Wait for it rather than
    # assuming pipeline_task is already set.
    await pipeline_started.wait()

    async def _forward_ice() -> None:
        """Trickle ICE candidates arrive at the PARENT (PATCH /connect/ice)
        for as long as negotiation continues; forward each into this
        process's own handler, which owns the actual peer connection."""
        loop = asyncio.get_event_loop()
        while True:
            item = await loop.run_in_executor(None, ice_queue.get)  # blocks a thread, not the loop
            if item is None:  # sentinel: parent is done sending, shut this down
                return
            candidates = [IceCandidate(**c) for c in item["candidates"]]
            patch = SmallWebRTCPatchRequest(pc_id=item["pc_id"], candidates=candidates)
            await handler.handle_patch_request(patch)

    ice_forward_task = asyncio.create_task(_forward_ice())

    try:
        await asyncio.wait_for(pipeline_task, timeout=MAX_CALL_LIFETIME_SECONDS)
    except TimeoutError:
        logger.warning(
            f"[CALL WORKER pid={mp.current_process().pid}] call exceeded "
            f"{MAX_CALL_LIFETIME_SECONDS}s safety cap, forcing exit"
        )
    finally:
        ice_forward_task.cancel()
