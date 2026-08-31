import asyncio
import multiprocessing as mp

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.deps import fetch_owned_bot
from app.models.user import User
from app.pipeline.call_worker import call_worker_main

router = APIRouter(tags=["voice"])

# Task 2.4 — pc_id -> (process, ice_queue) for every currently-live call.
# Populated in connect(), read by ice_candidate() to route trickle ICE to
# the right process, cleaned up by _reap_dead_calls() (started in main.py's
# lifespan). In-memory and single-process-API-server only — fine for now
# (matches the rest of this project's current scale), but if the API layer
# itself is ever run as multiple replicas, this registry needs to move to
# something shared (Redis) so any replica can route to any call's worker.
_active_calls: dict[str, tuple[mp.Process, mp.Queue]] = {}


async def reap_dead_calls_loop(interval_seconds: int = 10) -> None:
    """Background loop (started from main.py's lifespan) — removes finished
    or crashed calls from the registry and joins their process so it
    doesn't linger as a zombie. Without this, _active_calls only ever grows,
    and finished child processes are never actually reaped."""
    while True:
        await asyncio.sleep(interval_seconds)
        dead_pc_ids = [pc_id for pc_id, (proc, _) in _active_calls.items() if not proc.is_alive()]
        for pc_id in dead_pc_ids:
            proc, _ice_queue = _active_calls.pop(pc_id)
            proc.join(timeout=1)
            logger.info(f"[CALL] Cleaned up finished call pc_id={pc_id} (exitcode={proc.exitcode})")


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

    answer_queue: mp.Queue = mp.Queue()
    ice_queue: mp.Queue = mp.Queue()
    proc = mp.Process(
        target=call_worker_main,
        args=(bot_config, body.sdp, body.type, body.pc_id, answer_queue, ice_queue),
        daemon=True,
    )
    proc.start()

    loop = asyncio.get_event_loop()
    try:
        # answer_queue.get() is a blocking call — run it off the event loop
        # so the API server keeps serving other requests while this call's
        # process negotiates its WebRTC connection.
        answer = await loop.run_in_executor(None, lambda: answer_queue.get(timeout=15))
    except Exception as e:
        proc.terminate()
        raise HTTPException(status_code=504, detail="Call setup timed out") from e

    _active_calls[answer["pc_id"]] = (proc, ice_queue)
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

    _proc, ice_queue = entry
    ice_queue.put({
        "pc_id": body.pc_id,
        "candidates": [c.model_dump() for c in body.candidates],
    })
    return {"status": "ok"}
