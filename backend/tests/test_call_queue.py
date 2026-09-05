"""Task 4.2 — the Redis Streams call queue.

This module is NOT wired into POST /connect (see call_queue.py's own
docstring for why: the synchronous WebRTC handshake and a genuinely
separate worker fleet don't mix without an API/client change this project
hasn't made). What's tested here is that the mechanism itself is correct,
against a real Redis Streams implementation (fakeredis, in-memory) rather
than a mock that could quietly assert something Redis doesn't actually
guarantee: two workers never claim the same job, and a job whose worker
died is recovered rather than lost.
"""

import fakeredis.aioredis as fakeredis
import pytest

from app.pipeline import call_queue

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Captured at import time, before any test's fixture can monkeypatch
# call_queue._client — this is the real function, used by the one test
# below that checks its behaviour with no fake Redis substituted in.
_real_client_fn = call_queue._client


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    """One shared fake server per test, reached by a fresh client each call
    — matching call_queue._client()'s real "new client per call" shape,
    just backed by fakeredis instead of a real connection."""
    server = fakeredis.FakeServer()

    def _client():
        return fakeredis.FakeRedis(server=server, decode_responses=True)

    monkeypatch.setattr(call_queue, "_client", _client)
    monkeypatch.setattr(call_queue.settings, "redis_url", "redis://fake/0")
    yield


BOT_CONFIG = {"bot_name": "Auris", "bot_id": "bot-1", "user_id": "user-1"}


async def test_a_queued_call_can_be_claimed():
    await call_queue.enqueue(BOT_CONFIG, sdp="v=0...", sdp_type="offer", pc_id=None)

    claimed = await call_queue.claim_one("worker-1", block_ms=100)

    assert claimed is not None
    assert claimed.bot_config == BOT_CONFIG
    assert claimed.sdp_type == "offer"


async def test_an_empty_queue_returns_none_rather_than_hanging():
    claimed = await call_queue.claim_one("worker-1", block_ms=50)
    assert claimed is None


async def test_two_workers_never_claim_the_same_job():
    """The actual guarantee task 4.2 asks for: 'use consumer groups so each
    job is claimed exactly once.' This is Redis's own guarantee, not
    application logic — the test exists to prove this module actually uses
    it correctly, not to re-implement it."""
    await call_queue.enqueue(BOT_CONFIG, sdp="offer-a", sdp_type="offer", pc_id=None)
    await call_queue.enqueue(BOT_CONFIG, sdp="offer-b", sdp_type="offer", pc_id=None)

    first = await call_queue.claim_one("worker-1", block_ms=100)
    second = await call_queue.claim_one("worker-2", block_ms=100)

    assert first.entry_id != second.entry_id
    assert {first.sdp, second.sdp} == {"offer-a", "offer-b"}

    # And a third worker finds nothing left.
    assert await call_queue.claim_one("worker-3", block_ms=50) is None


async def test_an_acked_job_is_not_reclaimed(monkeypatch):
    """Acking removes the job from the group's pending-entries list
    entirely, so it must not come back even with a zero idle threshold —
    the threshold is irrelevant to an entry that isn't pending at all."""
    monkeypatch.setattr(call_queue, "STALL_TIMEOUT_MS", 0)
    await call_queue.enqueue(BOT_CONFIG, sdp="offer", sdp_type="offer", pc_id=None)
    claimed = await call_queue.claim_one("worker-1", block_ms=100)

    await call_queue.ack(claimed.entry_id)
    recovered = await call_queue.reclaim_stalled("worker-2")

    assert recovered == [], "an acked job showed up as reclaimable"


async def test_a_dead_workers_unacked_job_is_recovered(monkeypatch):
    """The task's own explicit requirement: 'handle a worker dying mid-job
    so the job is not lost.' worker-1 claims it and never acks — standing
    in for a crash — and reclaim_stalled() must hand it to someone else."""
    monkeypatch.setattr(call_queue, "STALL_TIMEOUT_MS", 0)
    await call_queue.enqueue(BOT_CONFIG, sdp="offer", sdp_type="offer", pc_id=None)

    claimed = await call_queue.claim_one("worker-1", block_ms=100)
    assert claimed is not None  # worker-1 has it, and now "dies"

    recovered = await call_queue.reclaim_stalled("worker-2")

    assert len(recovered) == 1
    assert recovered[0].entry_id == claimed.entry_id
    assert recovered[0].sdp == "offer"


async def test_a_job_still_being_worked_is_not_reclaimed_out_from_under_its_worker(monkeypatch):
    """The other side of the same guarantee: reclaiming too eagerly would
    put two workers on one call, exactly what task 2.4's one-process-per-
    call design exists to prevent. STALL_TIMEOUT_MS is what stands between
    'the worker is still on it' and 'the worker died.'"""
    monkeypatch.setattr(call_queue, "STALL_TIMEOUT_MS", 90_000)
    await call_queue.enqueue(BOT_CONFIG, sdp="offer", sdp_type="offer", pc_id=None)
    await call_queue.claim_one("worker-1", block_ms=100)

    recovered = await call_queue.reclaim_stalled("worker-2")

    assert recovered == []


async def test_calling_without_redis_configured_fails_loudly(monkeypatch):
    """This module must never be reachable by accident — see its own
    docstring on why it isn't the live default. A clear error here beats a
    silent connection to whatever REDIS_URL happens to default to."""
    monkeypatch.setattr(call_queue.settings, "redis_url", "")

    with pytest.raises(RuntimeError, match="redis_url"):
        _real_client_fn()
