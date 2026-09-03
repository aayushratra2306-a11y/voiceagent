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
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from app.core.config import settings

# An orphaned process (pipeline stuck, cleanup never fired) must not live
# forever quietly eating memory — the manual's own explicit warning about
# this exact failure mode. 1 hour is generously above any real call length.
MAX_CALL_LIFETIME_SECONDS = 60 * 60


def _build_ice_servers() -> list[IceServer]:
    """Task 2.3 — ICE configuration for this call's peer connection.

    STUN alone (what this returns without TURN configured) handles most
    home and office networks. TURN is the relay that makes calls work from
    genuinely restrictive networks — corporate firewalls, symmetric NAT,
    some mobile carriers — where the two sides can never reach each other
    directly. Without it, those callers simply fail to connect, and there's
    nothing they can do about it on their end.

    Both are settings-driven so adding a TURN server later is a .env change,
    not a code change.
    """
    servers = [IceServer(urls=url.strip()) for url in settings.stun_servers.split(",") if url.strip()]

    if settings.turn_url:
        servers.append(
            IceServer(
                urls=settings.turn_url,
                username=settings.turn_username or None,
                credential=settings.turn_credential or None,
            )
        )
        logger.info(f"[CALL WORKER] ICE: {len(servers) - 1} STUN + TURN relay ({settings.turn_url})")
    else:
        logger.info(f"[CALL WORKER] ICE: {len(servers)} STUN, no TURN relay configured "
                    f"(callers behind restrictive networks may fail to connect — see Task 2.3)")

    return servers


def call_worker_main(
    bot_config: dict,
    sdp: str,
    sdp_type: str,
    pc_id: str | None,
    answer_queue: mp.Queue,
    ice_queue: mp.Queue,
) -> None:
    """Process entry point for a worker spawned FOR a specific call. Still
    used when the pool is empty — a burst of simultaneous calls degrades to
    the original behaviour rather than queueing behind the pool.

    Never raises back into the parent; any failure here is logged and simply
    ends this one process.
    """
    async def _run():
        run_voice_pipeline = await _prepare_worker()
        await _handle_call(
            run_voice_pipeline, bot_config, sdp, sdp_type, pc_id, answer_queue, ice_queue
        )

    try:
        asyncio.run(_run())
    except Exception:
        logger.exception(f"[CALL WORKER pid={mp.current_process().pid}] fatal error")


def pooled_worker_main(
    job_queue: mp.Queue,
    answer_queue: mp.Queue,
    ice_queue: mp.Queue,
    ready_event,
) -> None:
    """Process entry point for a POOLED worker: do the expensive startup
    first, then wait for a call to be assigned.

    Still exactly one call per process. That is deliberate — Task 2.4's whole
    point is that a crash, including a native fault in onnxruntime, can only
    ever take down the call it happened in. Reusing a process for a second
    call would trade that away for nothing, since the parent simply spawns a
    replacement the moment this one is claimed. The only thing pooling
    changes is WHEN the startup cost is paid: before the caller arrives
    rather than while they wait.

    The queues are created by the parent and passed at Process construction
    because a multiprocessing.Queue cannot itself be sent through a queue —
    which is why each pooled worker owns a private set rather than sharing
    one job queue across the pool.
    """
    async def _run():
        run_voice_pipeline = await _prepare_worker()
        ready_event.set()
        logger.info(f"[POOL] Worker pid={mp.current_process().pid} warm, waiting for a call")

        loop = asyncio.get_event_loop()
        # Blocks a thread, not the loop. No timeout: an idle worker should
        # wait indefinitely, and the parent kills it on shutdown (daemon).
        job = await loop.run_in_executor(None, job_queue.get)
        if job is None:  # shutdown sentinel
            return

        await _handle_call(
            run_voice_pipeline,
            job["bot_config"], job["sdp"], job["sdp_type"], job["pc_id"],
            answer_queue, ice_queue,
        )

    try:
        asyncio.run(_run())
    except Exception:
        logger.exception(f"[POOL WORKER pid={mp.current_process().pid}] fatal error")


async def _prepare_worker():
    """The expensive half of starting a worker: importing the pipeline stack
    and connecting to the database. Split out from handling a call so a
    pooled worker can do all of it BEFORE a call arrives — see
    pooled_worker_main below and the pool in app/api/connect.py.

    Measured on the deployed VM: this is 6.8s warm and 13.7s cold, and
    before pooling every caller waited through it after pressing Start.

    Returns run_voice_pipeline, because importing it is most of the cost and
    the caller should not pay for that import twice.
    """
    # Imported here, not at module top level: this module is imported by
    # the PARENT process too (connect.py needs call_worker_main as a spawn
    # target), and voice_pipeline.py pulls in the entire pipecat pipeline
    # stack (Silero VAD, Smart Turn's ONNX model, etc.) — no reason to pay
    # that import cost in the parent, which never runs a pipeline itself.
    # BUG FOUND 2026-08-31 (live test, first real call through this new
    # per-process path): every database write inside a call — saved
    # transcripts, order lookups, appointment booking — was silently
    # failing (TranscriptRecorder threw on every single turn:
    # beanie/odm/documents.py:1105, "Error processing frame"). Root cause:
    # this child is a genuinely fresh interpreter — it never inherited the
    # parent's init_beanie() call from main.py's lifespan, which only ever
    # ran in the parent.
    #
    # That "fresh interpreter" is now guaranteed rather than assumed:
    # connect.py forces the 'spawn' start method on every platform. It was
    # only spawn-by-default on Windows, and the fork default on Linux made
    # this very line fail on the first real deployment — see the long note
    # at the top of app/api/connect.py.
    # Every Document.insert()/.find_one() in here (TranscriptRecorder,
    # get_order_status, book_appointment) needs its own init in THIS
    # process. This is exactly the kind of gap Task 2.4's own "verified the
    # mechanism, not the WebRTC-specific path live" caveat was flagging.
    from app.db.mongo import init_db
    from app.models.appointment import Appointment
    from app.models.conversation import ConversationTurn
    from app.models.document import Document
    from app.models.order import Order
    from app.pipeline.voice_pipeline import run_voice_pipeline

    # Document is here for Task 2.10: the RAG processor resolves doc_id ->
    # filename to cite a source. Beanie raises CollectionWasNotInitialized
    # for any model missing from this list, so an omission here is the same
    # failure this whole init_db call exists to fix.
    await init_db([Order, Appointment, ConversationTurn, Document])
    return run_voice_pipeline


async def _handle_call(
    run_voice_pipeline,
    bot_config: dict,
    sdp: str,
    sdp_type: str,
    pc_id: str | None,
    answer_queue: mp.Queue,
    ice_queue: mp.Queue,
) -> None:
    """Everything that is specific to one call. Identical whether the process
    was spawned for this call or taken warm from the pool."""
    handler = SmallWebRTCRequestHandler(ice_servers=_build_ice_servers())
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
