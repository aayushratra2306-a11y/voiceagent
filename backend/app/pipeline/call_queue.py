"""Task 4.2 — a call queue and worker pool, built on Redis Streams.

Read this file's honest limit before its mechanism: it is NOT wired into
`POST /connect` today, and that is a considered decision, not an oversight.

`connect()` does a synchronous WebRTC handshake — the browser sends an SDP
offer and the HTTP response IS the SDP answer, negotiated within
CALL_SETUP_TIMEOUT_SECONDS (45s). Task 4.2's actual shape ("the API places a
job on a queue, a pool of worker PROCESSES on possibly a different MACHINE
claims it") only pays for itself once workers can live outside the API
process's own host — and getting there means the SDP answer has to travel
back across whatever moved the work off this machine (a second network hop
this project's current single-VM deployment does not have), or the
signaling has to become asynchronous (the browser polls for an answer
instead of getting one back inline) — a genuine API/client change, not a
queue behind the same endpoint. Wiring this in without that decision would
either silently do nothing (a queue of one machine claiming its own jobs
adds a hop for zero benefit) or quietly break call setup for a
"local-network-hop-that-isn't-there" reason nobody would think to check
first.

So this module exists to do the two things task 4.2 actually asks for —
CORRECTLY — ready for the day a worker fleet is a real decision:

  - `enqueue()` puts a call's setup details on a Redis Stream.
  - `claim_one()` uses a consumer group so two workers can never claim the
    same job (XREADGROUP's whole guarantee), and `ack()` marks a claimed job
    done. A worker that claims a job and then DIES before acking it is
    exactly XCLAIM's job to recover — `reclaim_stalled()` sweeps the
    group's pending-entries list for anything idle past a timeout and hands
    it to a live worker instead of losing it.

Inert unless settings.redis_url is set — see call_capacity.py and
rate_limit.py for the same pattern applied to the other two pieces of
Phase-4 shared state.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.core.config import settings

STREAM_KEY = "voiceagent:call_queue"
GROUP_NAME = "call-workers"

# How long a claimed-but-unacked job may sit before it's considered
# abandoned (the worker that claimed it died) and safe to hand to someone
# else. Generous relative to CALL_SETUP_TIMEOUT_SECONDS (45s in
# connect.py) — reclaiming a job that is actually still being worked would
# mean two workers on one call, exactly what task 2.4's one-call-per-user
# and one-call-per-process design exists to prevent.
STALL_TIMEOUT_MS = 90_000


@dataclass(frozen=True)
class QueuedCall:
    entry_id: str
    bot_config: dict[str, Any]
    sdp: str
    sdp_type: str
    pc_id: str | None


def _client():
    """A fresh async Redis client. Not cached at module level: this module
    is imported by code that may run in the API process (task 4.1's
    replicas) or a future standalone worker process, and each should own
    its own connection rather than share one built for a different
    process's lifetime."""
    if not settings.redis_url:
        raise RuntimeError(
            "call_queue requires settings.redis_url — this is the opt-in Phase 4 "
            "worker-fleet path, not the default connect() flow. See this module's "
            "docstring for why it isn't wired in yet."
        )
    import redis.asyncio as redis

    return redis.from_url(settings.redis_url, decode_responses=True)


async def ensure_group(client=None) -> None:
    """Idempotent — safe to call on every worker's startup. Consumer groups
    are a Redis-side object that must exist before XREADGROUP will work."""
    owns_client = client is None
    client = client or _client()
    try:
        try:
            await client.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
            logger.info(f"[QUEUE] Created consumer group '{GROUP_NAME}' on '{STREAM_KEY}'")
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise
    finally:
        if owns_client:
            await client.aclose()


async def enqueue(bot_config: dict, sdp: str, sdp_type: str, pc_id: str | None) -> str:
    """Place one call's setup details on the stream. Returns the stream
    entry id (Redis's own, monotonic per-stream — not the WebRTC pc_id,
    which does not exist yet at this point for a brand-new call)."""
    client = _client()
    try:
        await ensure_group(client)
        entry_id = await client.xadd(STREAM_KEY, {
            "bot_config": json.dumps(bot_config),
            "sdp": sdp,
            "sdp_type": sdp_type,
            "pc_id": pc_id or "",
        })
        return entry_id
    finally:
        await client.aclose()


async def claim_one(consumer_name: str, block_ms: int = 5000) -> QueuedCall | None:
    """One worker's turn to ask for a job. XREADGROUP's own guarantee is
    the entire point: if two workers call this at once, each gets a
    DIFFERENT entry (or one gets none) — Redis, not application code,
    enforces "claimed exactly once."

    Blocks up to block_ms waiting for a job before returning None, so a
    worker loop calling this repeatedly is a clean wait rather than a busy
    poll.
    """
    client = _client()
    try:
        await ensure_group(client)
        result = await client.xreadgroup(
            GROUP_NAME, consumer_name, {STREAM_KEY: ">"}, count=1, block=block_ms,
        )
        if not result:
            return None
        _stream, entries = result[0]
        entry_id, fields = entries[0]
        return QueuedCall(
            entry_id=entry_id,
            bot_config=json.loads(fields["bot_config"]),
            sdp=fields["sdp"],
            sdp_type=fields["sdp_type"],
            pc_id=fields["pc_id"] or None,
        )
    finally:
        await client.aclose()


async def ack(entry_id: str) -> None:
    """The job is done (the call started, or was abandoned deliberately —
    never call this for a job that might still need retrying). Removes it
    from the group's pending-entries list so reclaim_stalled() never
    revisits it."""
    client = _client()
    try:
        await client.xack(STREAM_KEY, GROUP_NAME, entry_id)
    finally:
        await client.aclose()


async def reclaim_stalled(consumer_name: str) -> list[QueuedCall]:
    """Task 4.2's own requirement: "handle a worker dying mid-job so the job
    is not lost." A job claimed via claim_one() but never ack()'d — because
    the worker holding it crashed — sits in the group's pending-entries list
    until something claims it again. This is that something: anything idle
    longer than STALL_TIMEOUT_MS is handed to `consumer_name` instead of
    staying lost forever.

    Meant to be called periodically by a live worker (or a small supervisor
    process), not on every claim_one() — that would turn one dead worker
    into every worker fighting over its leftovers on every single poll.
    """
    client = _client()
    try:
        recovered: list[QueuedCall] = []
        cursor = "0-0"
        # seen_cursors is a defensive backstop, not something real Redis
        # should ever trigger: XAUTOCLAIM's documented contract is that the
        # cursor becomes "0-0" once a full pass completes. It is here
        # because at least one in-memory test double for Redis Streams has
        # been observed replaying the same non-"0-0" cursor (and the same
        # already-claimed entries) forever — without this, a quirk in a TEST
        # backend would hang whatever calls this in production against the
        # real thing. Checked BEFORE processing a batch, not after, so a
        # repeated cursor is skipped rather than double-counted.
        seen_cursors: set[str] = set()
        while True:
            pending = await client.xautoclaim(
                STREAM_KEY, GROUP_NAME, consumer_name, min_idle_time=STALL_TIMEOUT_MS,
                start_id=cursor, count=10,
            )
            new_cursor, entries, _deleted = pending
            if new_cursor in seen_cursors:
                break
            seen_cursors.add(new_cursor)

            for entry_id, fields in entries:
                logger.warning(f"[QUEUE] Reclaimed stalled job {entry_id} for {consumer_name} "
                                f"— its previous worker likely died mid-call")
                recovered.append(QueuedCall(
                    entry_id=entry_id,
                    bot_config=json.loads(fields["bot_config"]),
                    sdp=fields["sdp"],
                    sdp_type=fields["sdp_type"],
                    pc_id=fields["pc_id"] or None,
                ))

            cursor = new_cursor
            if cursor == "0-0" or not entries:
                break
        return recovered
    finally:
        await client.aclose()


def new_consumer_name() -> str:
    """A stable-enough identity for one worker process's lifetime. Random
    rather than the OS pid: pids get reused, and two different processes
    sharing a consumer name would each think the other's pending entries
    are their own to reclaim."""
    return f"worker-{uuid.uuid4().hex[:12]}"
