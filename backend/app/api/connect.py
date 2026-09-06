import asyncio
import multiprocessing as mp
import threading
import time
from dataclasses import dataclass
from multiprocessing.synchronize import Event as EventClass

import psutil
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.call_capacity import (
    active_call_count,
    release_call_slot,
    try_acquire_call_slot,
)
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
    # Task 3.7 — parent -> child, for a payment webhook that lands here
    # minutes after the link was sent and needs to reach the call that is
    # still in progress. See get_payment_queue() below.
    payment_queue: "mp.Queue | None" = None
    # Task 4.5 — this call's capacity slot, released when the call ends.
    # Held here rather than counted, so the slot given back is always the
    # exact one this call took; see call_capacity.py on why slots are named.
    slot_token: str | None = None


_active_calls: dict[str, _ActiveCall] = {}


async def _end_previous_calls_for(user_id: str) -> None:
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
        # Task 4.5 — freed immediately, not left for reap_dead_calls_loop's
        # next pass. The exact case this matters for: the same user
        # reconnecting should never be blocked by their OWN stale call still
        # holding the slot their new one needs.
        await release_call_slot(call.slot_token)


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
    # Task 3.7 — created up front like the others, for the same reason: a
    # multiprocessing.Queue has to be handed over at Process construction
    # and cannot be sent through another queue afterwards.
    payment_queue: "mp.Queue | None" = None


_idle_pool: list[_PooledWorker] = []

# Task 4.3 — the pool's current target size, grown and shrunk between
# call_worker_pool_min and call_worker_pool_max by maintain_worker_pool_loop.
# Seeded from the pre-4.3 fixed setting (call_worker_pool_size) so an
# operator's existing .env keeps behaving exactly as before until they
# actually set the new min/max — clamped into range in case that old value
# sits outside whatever min/max ends up configured.
_pool_target = min(max(settings.call_worker_pool_size, settings.call_worker_pool_min),
                    settings.call_worker_pool_max)

# Task 4.3 — set by connect() the moment a call has to fall back to a cold
# spawn because the pool was empty: unambiguous evidence that demand
# exceeded supply, checked and cleared once per autoscale tick.
_pool_exhausted_since_last_check = False

# How many consecutive quiet ticks (the pool was not exhausted) before
# shrinking by one. Short growth reaction (immediate, on any exhaustion) but
# slower to shrink — the manual's own tradeoff for autoscaling in general:
# reacting instantly to a burst avoids callers ever seeing it; shrinking
# eagerly would then just re-grow on the very next call and repeat forever.
_SHRINK_AFTER_QUIET_TICKS = 4


def next_pool_target(
    current_target: int,
    exhausted: bool,
    quiet_ticks: int,
    available_memory_mb: float,
    pool_min: int,
    pool_max: int,
    min_free_memory_mb: int,
    # Rough per-worker cost — see the latency note in config.py: a warm
    # worker holds an imported pipecat stack before it has done anything
    # call-specific, measured at roughly 300MB.
    worker_memory_mb: int = 300,
) -> int:
    """Task 4.3's actual decision, pulled out as a pure function so it can be
    tested without spawning a single real process.

    Three rules, in priority order:
      1. Demand beat supply since the last check (a cold-spawn fallback
         happened) -> grow by one, but ONLY if there is comfortably enough
         memory for another warm worker. Growing into an out-of-memory kill
         during live calls is a worse outcome than the caller who paid the
         slow cold-spawn path once.
      2. The pool sat unclaimed for several checks running -> shrink by one.
         Paying for idle capacity all night is exactly what the manual's own
         framing of this task warns against.
      3. Otherwise, hold steady.

    Both directions are clamped to [pool_min, pool_max] — pool_max exists
    specifically so unbounded growth under a sustained burst can't fill the
    machine with idle workers once the burst passes, and pool_min exists so
    a quiet server never scales all the way down to zero and starts paying
    the full cold-start cost on every single call.
    """
    # A max below the min is a configuration typo, not an instruction to
    # do something clever. Clamped rather than raised: a bad number in a
    # .env should not stop a server booting, and the floor is the value
    # with a real cost attached (drop below it and every caller starts
    # paying the 13.7s cold start).
    pool_max = max(pool_min, pool_max)

    if exhausted:
        if available_memory_mb < min_free_memory_mb + worker_memory_mb:
            logger.warning(
                f"[POOL] Demand exceeds supply but only {available_memory_mb:.0f}MB is free "
                f"— holding at {current_target} rather than risking an OOM kill"
            )
            return current_target
        return min(current_target + 1, pool_max)

    if quiet_ticks >= _SHRINK_AFTER_QUIET_TICKS and current_target > pool_min:
        return current_target - 1

    return current_target


def _spawn_pooled_worker() -> _PooledWorker:
    job_queue: mp.Queue = _MP.Queue()
    answer_queue: mp.Queue = _MP.Queue()
    ice_queue: mp.Queue = _MP.Queue()
    payment_queue: mp.Queue = _MP.Queue()
    ready = _MP.Event()
    proc = _MP.Process(
        target=pooled_worker_main,
        args=(job_queue, answer_queue, ice_queue, ready, payment_queue),
        daemon=True,
    )
    proc.start()
    return _PooledWorker(
        proc, job_queue, answer_queue, ice_queue, ready, time.monotonic(), payment_queue
    )


# Serialises every change to _idle_pool's SIZE. Two things call
# _top_up_pool: the maintenance loop, and connect() on every single call —
# both via run_in_executor, so both on real threads, and _shrink_pool_by_one
# runs there too. Without this lock they interleave on the check: two
# threads each read len(_idle_pool) == 1 against a target of 2, and each
# spawns one, leaving three. Every overshoot worker is ~300MB held on a 4GB
# VM, which is precisely the outcome pool_min_free_memory_mb exists to
# prevent — and a race that walks straight past the guard is worse than no
# guard, because the number in the config stops meaning anything.
_pool_lock = threading.Lock()


def _top_up_pool() -> None:
    """Bring the pool to its current target. Blocking (Process.start spawns a
    fresh interpreter), so callers on the event loop run it in an executor."""
    with _pool_lock:
        while len(_idle_pool) < _pool_target:
            _idle_pool.append(_spawn_pooled_worker())


def _shrink_pool_by_one() -> None:
    """The other half of _top_up_pool: retire one warm worker when the pool
    is bigger than it needs to be. Terminated rather than sent the shutdown
    sentinel — it is sitting in run_in_executor(None, job_queue.get) (see
    pooled_worker_main), which nothing but an actual item on that queue or
    the process dying will interrupt."""
    with _pool_lock:
        if len(_idle_pool) <= _pool_target:
            # Re-checked under the lock: a call claimed a worker (or a
            # top-up ran) between the loop deciding to shrink and this
            # getting the lock, and the pool is already the size it should
            # be. Retiring one anyway would push it below target and make
            # the next caller pay a cold start for nothing.
            return
        worker = _idle_pool.pop()
    worker.process.terminate()
    worker.process.join(timeout=1)
    logger.info(f"[POOL] Retired idle worker pid={worker.process.pid} — demand has settled")


async def maintain_worker_pool_loop(interval_seconds: int = 15) -> None:
    """Background loop (started from main.py's lifespan). Fills the pool to
    its current target at startup, replaces workers that die while idle (a
    worker that fails during import would otherwise silently shrink the pool
    to nothing with every call quietly falling back to the slow path), and —
    task 4.3 — grows or shrinks that target itself based on recent demand
    and available memory. See next_pool_target()'s docstring for the actual
    decision.
    """
    global _pool_target, _pool_exhausted_since_last_check
    loop = asyncio.get_event_loop()
    quiet_ticks = 0

    while True:
        dead = [w for w in _idle_pool if not w.process.is_alive()]
        for w in dead:
            _idle_pool.remove(w)
            w.process.join(timeout=1)
            logger.warning(f"[POOL] Idle worker pid={w.process.pid} died before use, replacing")

        exhausted, _pool_exhausted_since_last_check = _pool_exhausted_since_last_check, False
        quiet_ticks = 0 if exhausted else quiet_ticks + 1

        available_mb = psutil.virtual_memory().available / (1024 * 1024)
        new_target = next_pool_target(
            _pool_target, exhausted, quiet_ticks, available_mb,
            settings.call_worker_pool_min, settings.call_worker_pool_max,
            settings.pool_min_free_memory_mb,
        )
        if new_target != _pool_target:
            logger.info(f"[POOL] Target {_pool_target} -> {new_target} "
                        f"(exhausted={exhausted}, quiet_ticks={quiet_ticks})")
            _pool_target = new_target

        before = len(_idle_pool)
        if len(_idle_pool) > _pool_target:
            await loop.run_in_executor(None, _shrink_pool_by_one)
        else:
            await loop.run_in_executor(None, _top_up_pool)
        if len(_idle_pool) != before:
            logger.info(f"[POOL] {len(_idle_pool)} warm worker(s) ready (target {_pool_target})")

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
            # Task 4.5 — the normal path a slot is freed: the call simply
            # ended. _end_previous_calls_for above covers the other path
            # (this same user starting a new call before this loop's next
            # pass would have caught the old one).
            await release_call_slot(call.slot_token)
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
    # Before anything else, including the database lookup below: this
    # caller gets exactly one live pipeline. A previous one still running
    # would otherwise keep hearing them and keep answering over the new one
    # — see _end_previous_calls_for(). Also frees that call's capacity slot
    # immediately, ahead of the check below, so a user reconnecting is
    # never blocked by their own stale call.
    await _end_previous_calls_for(str(current_user.id))

    # Task 4.5 — the hard ceiling, checked before the bot lookup on purpose:
    # a system already at capacity should not spend a database round trip
    # finding that out. Checked, and its increment made, in one atomic step
    # (see call_capacity.py) — a check-then-increment done in two separate
    # steps is exactly how two requests both squeeze through when only one
    # slot is actually free.
    slot_token = await try_acquire_call_slot()
    if slot_token is None:
        current = await active_call_count()
        logger.warning(
            f"[CAPACITY] Refusing a new call — at the cap of "
            f"{settings.max_concurrent_calls} ({current} active)"
        )
        raise HTTPException(
            status_code=503,
            detail="This system is at capacity right now. Please try again in a moment.",
        )

    try:
        # Task 2.6: bot_id here comes from the request body, not the URL
        # path, so it uses fetch_owned_bot directly rather than the
        # get_owned_bot FastAPI dependency (which resolves bot_id from a
        # path parameter).
        bot = await fetch_owned_bot(body.bot_id, current_user)
    except BaseException:
        # The slot was claimed above; an unknown/unowned bot_id must not
        # hold it forever.
        await release_call_slot(slot_token)
        raise

    # Task 2.4 — plain picklable data passed across the process boundary as
    # multiprocessing.Process args, not the ORM object itself.
    bot_config = {
        "bot_name": bot.name,
        "system_prompt": bot.system_prompt,
        "voice_id": bot.voice_id,
        "llm_model": bot.llm_model,
        "language": bot.language,
        "bot_id": str(bot.id),
        # Task 3.8 — the bot's owner, not the caller. A webhook fires to
        # whichever customer of THIS platform configured it (Bot.user_id),
        # so their own system hears about their own bot's events.
        "user_id": str(bot.user_id),
        # Task 6.2 — which sensitive-data categories to mask out of this
        # bot's transcripts. list(...) rather than the ODM field directly:
        # bot_config crosses a multiprocessing.Process boundary (see the
        # comment above), and everything in it must be plain picklable data.
        "redact_transcripts": list(bot.redact_transcripts),
        # Task 6.3 — recording_retention_days is NOT here: it is read by
        # the scheduled purge job (app/services/retention.py) straight off
        # the Bot document, not something the call itself needs to know.
        "recording_enabled": bot.recording_enabled,
        "consent_announcement": bot.consent_announcement,
        # Task 6.1 — topics this bot must never discuss. list(...) for the
        # same reason as redact_transcripts above: bot_config crosses a
        # multiprocessing.Process boundary and must be plain picklable data.
        "guardrail_topics": list(bot.guardrail_topics),
    }

    loop = asyncio.get_event_loop()

    # Task 4.5 — from here until the call is registered in _active_calls,
    # any failure must release the slot just acquired above, or it leaks
    # forever (nothing else in the system knows this call ever existed).
    try:
        # Take a warm worker if one is waiting. The pool is topped up
        # straight afterwards, off the event loop, so the replacement is
        # already importing while this call negotiates.
        #
        # try/except rather than the bare `if _idle_pool` this used to be:
        # _shrink_pool_by_one() (task 4.3) runs in an executor THREAD and
        # pops from the other end of the same list, so between the check
        # and the pop the last worker can legitimately disappear. Falling
        # through to a cold spawn is the correct answer to that; a 500 for
        # the caller is not.
        try:
            worker = _idle_pool.pop(0)
        except IndexError:
            worker = None

        if worker is not None:
            proc, answer_queue, ice_queue = worker.process, worker.answer_queue, worker.ice_queue
            payment_queue = worker.payment_queue
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
            # Pool exhausted — a burst of simultaneous calls. Fall back to
            # the original behaviour: spawn a worker for this call. Slower
            # to answer, but it answers, which beats queueing behind the
            # pool.
            #
            # Task 4.3 — this is the demand signal next_pool_target() acts
            # on: unambiguous evidence that supply fell short this tick.
            global _pool_exhausted_since_last_check
            _pool_exhausted_since_last_check = True
            logger.warning("[POOL] Empty, spawning a cold worker for this call")
            answer_queue = _MP.Queue()
            ice_queue = _MP.Queue()
            payment_queue = _MP.Queue()
            proc = _MP.Process(
                target=call_worker_main,
                args=(
                    bot_config, body.sdp, body.type, body.pc_id,
                    answer_queue, ice_queue, payment_queue,
                ),
                daemon=True,
            )
            proc.start()

        try:
            # answer_queue.get() is a blocking call — run it off the event
            # loop so the API server keeps serving other requests while
            # this call's process negotiates its WebRTC connection.
            answer = await loop.run_in_executor(
                None, lambda: answer_queue.get(timeout=CALL_SETUP_TIMEOUT_SECONDS)
            )
        except Exception as e:
            proc.terminate()
            raise HTTPException(status_code=504, detail="Call setup timed out") from e
    except BaseException:
        await release_call_slot(slot_token)
        raise

    _active_calls[answer["pc_id"]] = _ActiveCall(
        proc, ice_queue, str(current_user.id), payment_queue, slot_token
    )
    logger.info(f"[CALL] Started call worker pid={proc.pid} pc_id={answer['pc_id']} bot={bot.name}")
    return answer


def get_payment_queue(pc_id: str) -> "mp.Queue | None":
    """Task 3.7 — the channel into a specific live call, or None if it has
    already ended.

    Exposed as a function rather than letting the payments route reach into
    `_active_calls` directly: this registry's shape is an implementation
    detail of the process model (and the comment above it notes it will
    have to move to Redis if the API is ever run as more than one replica).
    One accessor is one place to change when that happens.
    """
    call = _active_calls.get(pc_id)
    return call.payment_queue if call else None


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
