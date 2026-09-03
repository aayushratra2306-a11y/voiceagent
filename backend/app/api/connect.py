import asyncio
import multiprocessing as mp
import time
from dataclasses import dataclass
from multiprocessing.synchronize import Event as EventClass

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.deps import fetch_owned_bot
from app.models.user import User
from app.pipeline.call_worker import call_worker_main, pooled_worker_main

router = APIRouter(tags=["voice"])

# Task 2.4 — 'spawn', explicitly, on every platform.
#
# FOUND 2026-09-01, first Linux deployment: multiprocessing's default start
# method is 'spawn' on Windows but 'fork' on Linux, and every line of this
# per-call-process design had only ever run under spawn. Under fork the
# child inherits the parent's memory, so app/db/mongo.py's module-level
# Motor client arrives already bound to the PARENT's event loop. The
# child's own init_db() then dies with "got Future attached to a different
# loop", the SDP answer is never queued, and connect() below 504s — every
# call failing before a single audio frame moves, on a code path that works
# perfectly in Windows dev.
#
# Forcing spawn makes Linux behave exactly like the environment this was
# written and tested in, and sidesteps fork-safety as a category: pymongo
# is explicitly not fork-safe, and neither are the onnxruntime and aiortc
# thread pools the pipeline stands up.
_MP = mp.get_context("spawn")

# A spawned child is a genuinely fresh interpreter, so it re-imports the
# whole pipecat stack (Silero VAD, Smart Turn's ONNX model, torch) before
# it can answer. On a small cloud VM with a cold page cache that is well
# over the 15s this used to allow.
CALL_SETUP_TIMEOUT_SECONDS = 45

# Task 2.4 — pc_id -> the currently-live call for it. Populated in
# connect(), read by ice_candidate() to route trickle ICE to the right
# process, cleaned up by _reap_dead_calls() (started in main.py's
# lifespan). In-memory and single-process-API-server only — fine for now
# (matches the rest of this project's current scale), but if the API layer
# itself is ever run as multiple replicas, this registry needs to move to
# something shared (Redis) so any replica can route to any call's worker.
@dataclass
class _ActiveCall:
    process: mp.Process
    ice_queue: mp.Queue
    user_id: str


_active_calls: dict[str, _ActiveCall] = {}


def _end_previous_calls_for(user_id: str) -> None:
    """Enforce one live call per user, killing any earlier one.

    FOUND 2026-09-03, from a call the user reported as "not a proper call".
    The backend had no such rule, and the logs showed the consequence
    plainly: two pipelines, two conversation IDs, two turn counters
    (5->6->7->8 alongside 4->5->6), both transcribing the same spoken words
    within milliseconds of each other, each running its own RAG lookup and
    its own LLM completion against its own private history, and each
    speaking its own answer into the same call. The caller heard two bots at
    once — one replying in Hindi that it could hear them, the other in
    English that it was "just sending you text responses, so you won't
    actually hear a voice from me".

    The stale pipeline is not idle in this state, which is what makes it so
    disruptive: it still holds a live inbound media track, so it keeps
    hearing the caller and keeps answering. It also does not clean itself up
    promptly — the orphan in that log sat on an open Deepgram stream for 209
    seconds and was only reaped when aiortc's own no-audio timeout finally
    fired, minutes after the caller had hung up.

    A browser is not required to misbehave badly for this to happen: any
    path that reaches startSession() twice leaves the first RTCPeerConnection
    live but unreachable from the page's own ref, so the browser can neither
    close it nor even know it exists. That is fixed on the frontend too, but
    the rule belongs here as well — the server should not be willing to run
    two pipelines for one caller no matter what the client does, and this
    side is the one that still holds when the client is a stale tab, a
    reloaded page, or a network that dropped without a close handshake.
    """
    stale = [pc_id for pc_id, call in _active_calls.items() if call.user_id == user_id]
    for pc_id in stale:
        call = _active_calls.pop(pc_id)
        if call.process.is_alive():
            call.process.terminate()
            logger.warning(
                f"[CALL] Ending previous live call pc_id={pc_id} pid={call.process.pid} "
                f"— same user started a new one"
            )
        call.process.join(timeout=1)


# Latency (2026-09-03) — the pre-warmed worker pool.
#
# Task 2.4 spawns a fresh interpreter per call, and that process has to
# import the whole pipecat stack and connect to MongoDB before it can even
# answer the WebRTC offer. Measured on the deployed VM: 6.8s warm, 13.7s
# cold, and the caller sat through all of it after pressing Start.
#
# The work has to happen; it does not have to happen while someone waits. So
# a few workers do it in advance and idle until a call arrives.
#
# Still ONE CALL PER PROCESS. That is the point of Task 2.4 and pooling does
# not weaken it: a claimed worker is replaced immediately, so a crash still
# only ever takes down the call it happened in. The only thing that changes
# is when the startup cost is paid.
#
# Each worker owns a private set of queues rather than sharing one job queue,
# because a multiprocessing.Queue cannot be sent through another queue — it
# has to be handed over at Process construction. That also means the parent
# always knows exactly which process took which call, which the ICE routing
# and the reaper both depend on.
@dataclass
class _PooledWorker:
    process: mp.Process
    job_queue: mp.Queue
    answer_queue: mp.Queue
    ice_queue: mp.Queue
    ready: EventClass
    spawned_at: float


_idle_pool: list[_PooledWorker] = []


def _spawn_pooled_worker() -> _PooledWorker:
    job_queue: mp.Queue = _MP.Queue()
    answer_queue: mp.Queue = _MP.Queue()
    ice_queue: mp.Queue = _MP.Queue()
    ready = _MP.Event()
    proc = _MP.Process(
        target=pooled_worker_main,
        args=(job_queue, answer_queue, ice_queue, ready),
        daemon=True,
    )
    proc.start()
    return _PooledWorker(proc, job_queue, answer_queue, ice_queue, ready, time.monotonic())


def _top_up_pool() -> None:
    """Bring the pool back to size. Blocking (Process.start forks a process),
    so callers on the event loop run it in an executor."""
    while len(_idle_pool) < settings.call_worker_pool_size:
        _idle_pool.append(_spawn_pooled_worker())


async def maintain_worker_pool_loop(interval_seconds: int = 15) -> None:
    """Background loop (started from main.py's lifespan). Fills the pool at
    startup and replaces workers that die while idle — a worker that fails
    during import would otherwise silently shrink the pool to nothing and
    every call would quietly fall back to the slow path."""
    loop = asyncio.get_event_loop()
    while True:
        dead = [w for w in _idle_pool if not w.process.is_alive()]
        for w in dead:
            _idle_pool.remove(w)
            w.process.join(timeout=1)
            logger.warning(f"[POOL] Idle worker pid={w.process.pid} died before use, replacing")

        before = len(_idle_pool)
        await loop.run_in_executor(None, _top_up_pool)
        if len(_idle_pool) != before:
            logger.info(f"[POOL] {len(_idle_pool)} warm worker(s) ready")

        await asyncio.sleep(interval_seconds)


async def reap_dead_calls_loop(interval_seconds: int = 10) -> None:
    """Background loop (started from main.py's lifespan) — removes finished
    or crashed calls from the registry and joins their process so it
    doesn't linger as a zombie. Without this, _active_calls only ever grows,
    and finished child processes are never actually reaped."""
    while True:
        await asyncio.sleep(interval_seconds)
        dead_pc_ids = [
            pc_id for pc_id, call in _active_calls.items() if not call.process.is_alive()
        ]
        for pc_id in dead_pc_ids:
            call = _active_calls.pop(pc_id)
            call.process.join(timeout=1)
            logger.info(
                f"[CALL] Cleaned up finished call pc_id={pc_id} "
                f"(exitcode={call.process.exitcode})"
            )


class WebRTCOffer(BaseModel):
    bot_id: str
    sdp: str
    type: str
    pc_id: str | None = None


class IceCandidateBody(BaseModel):
    candidate: str
    sdp_mid: str
    sdp_mline_index: int


class IcePatchBody(BaseModel):
    pc_id: str
    candidates: list[IceCandidateBody]


@router.get("/connect/ice-servers")
async def ice_servers(current_user: User = Depends(get_current_user)):
    """Task 2.3 — ICE configuration for the BROWSER side of the call.

    FOUND 2026-09-01: task 2.3 made the backend's ICE config settings-driven
    and left the frontend with a hardcoded STUN-only list, so the TURN relay
    this project stands up was invisible to the one peer that actually needs
    it — the caller. STUN only tells each side its own public address, which
    is plenty on an ordinary connection and useless behind symmetric NAT or
    a carrier-grade-NAT mobile network. Serving this from settings means
    both peers read one configuration and a TURN change stays a .env edit.

    Authenticated on purpose: TURN credentials are a shared secret, and an
    open relay is someone else's bandwidth on your bill.
    """
    servers: list[dict] = [
        {"urls": url.strip()} for url in settings.stun_servers.split(",") if url.strip()
    ]
    if settings.turn_url:
        servers.append({
            "urls": settings.turn_url,
            "username": settings.turn_username,
            "credential": settings.turn_credential,
        })
    return {"iceServers": servers}


@router.post("/connect")
async def connect(body: WebRTCOffer, current_user: User = Depends(get_current_user)):
    # Task 2.6: bot_id here comes from the request body, not the URL path,
    # so it uses fetch_owned_bot directly rather than the get_owned_bot
    # FastAPI dependency (which resolves bot_id from a path parameter).
    bot = await fetch_owned_bot(body.bot_id, current_user)

    # Task 2.4 — plain picklable data passed across the process boundary as
    # multiprocessing.Process args, not the ORM object itself.
    bot_config = {
        "bot_name": bot.name,
        "system_prompt": bot.system_prompt,
        "voice_id": bot.voice_id,
        "llm_model": bot.llm_model,
        "language": bot.language,
        "bot_id": str(bot.id),
    }

    # Before anything else: this caller gets exactly one live pipeline. A
    # previous one still running would otherwise keep hearing them and keep
    # answering over the new one — see _end_previous_calls_for().
    _end_previous_calls_for(str(current_user.id))

    loop = asyncio.get_event_loop()

    # Take a warm worker if one is waiting. The pool is topped up straight
    # afterwards, off the event loop, so the replacement is already importing
    # while this call negotiates.
    worker = _idle_pool.pop(0) if _idle_pool else None

    if worker is not None:
        proc, answer_queue, ice_queue = worker.process, worker.answer_queue, worker.ice_queue
        worker.job_queue.put({
            "bot_config": bot_config,
            "sdp": body.sdp,
            "sdp_type": body.type,
            "pc_id": body.pc_id,
        })
        warm = "warm" if worker.ready.is_set() else "still starting"
        logger.info(f"[POOL] Claimed {warm} worker pid={proc.pid} ({len(_idle_pool)} left)")
        loop.run_in_executor(None, _top_up_pool)
    else:
        # Pool exhausted — a burst of simultaneous calls. Fall back to the
        # original behaviour: spawn a worker for this call. Slower to answer,
        # but it answers, which beats making the caller queue behind the pool.
        logger.warning("[POOL] Empty, spawning a cold worker for this call")
        answer_queue = _MP.Queue()
        ice_queue = _MP.Queue()
        proc = _MP.Process(
            target=call_worker_main,
            args=(bot_config, body.sdp, body.type, body.pc_id, answer_queue, ice_queue),
            daemon=True,
        )
        proc.start()

    try:
        # answer_queue.get() is a blocking call — run it off the event loop
        # so the API server keeps serving other requests while this call's
        # process negotiates its WebRTC connection.
        answer = await loop.run_in_executor(
            None, lambda: answer_queue.get(timeout=CALL_SETUP_TIMEOUT_SECONDS)
        )
    except Exception as e:
        proc.terminate()
        raise HTTPException(status_code=504, detail="Call setup timed out") from e

    _active_calls[answer["pc_id"]] = _ActiveCall(proc, ice_queue, str(current_user.id))
    logger.info(f"[CALL] Started call worker pid={proc.pid} pc_id={answer['pc_id']} bot={bot.name}")
    return answer


@router.post("/connect/ice")
async def ice_candidate(body: IcePatchBody, current_user: User = Depends(get_current_user)):
    entry = _active_calls.get(body.pc_id)
    if entry is None:
        # The call may have already ended (or this is a stray/late candidate
        # from a connection that failed setup) — not an error worth 4xx-ing
        # the client over, same as the original handler's tolerant behavior.
        logger.debug(f"[CALL] ICE candidate for unknown/ended pc_id={body.pc_id}, ignoring")
        return {"status": "ok"}

    entry.ice_queue.put({
        "pc_id": body.pc_id,
        "candidates": [c.model_dump() for c in body.candidates],
    })
    return {"status": "ok"}
