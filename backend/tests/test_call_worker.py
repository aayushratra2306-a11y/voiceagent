# Task 2.4 — regression test for a real bug found in the first live call
# through the new per-process architecture: multiprocessing uses 'spawn' on
# Windows, so a call worker child is a genuinely fresh interpreter that
# never inherited the parent's init_beanie() call from main.py's lifespan.
# Every database write inside a call (saved transcripts, order lookups,
# appointment booking) was silently failing until call_worker.py's
# _worker_main started calling init_db() itself. This spawns a REAL
# separate process (not a mock) and proves a database write succeeds in
# it — exactly the failure mode that shipped, caught live, and must not
# come back.
#
# Must be a real multiprocessing.Process, not just calling the coroutine
# in-process: the whole bug was specific to process boundaries (spawn
# re-imports everything fresh) — an in-process call would never have
# caught it, same as it wasn't caught by any earlier test.
import multiprocessing as mp
import os

import pytest

os.environ.setdefault("DB_NAME", "voiceagent_test")

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _child_writes_to_db(result_queue: mp.Queue) -> None:
    """Runs in a genuinely separate spawned process — mirrors exactly what
    call_worker.py's _worker_main does on startup before touching any
    Document model. Self-contained: inserts its own Order rather than
    relying on app/db/seed.py's seeded data, which only ever runs against
    the real 'voiceagent' database, not this test's separate one."""
    import asyncio

    async def run():
        from app.db.mongo import init_db
        from app.models.conversation import ConversationTurn
        from app.models.order import Order

        await init_db([Order, ConversationTurn])

        turn = ConversationTurn(session_id="test-call-worker", bot_id="x", bot_name="x")
        await turn.insert()

        order = Order(
            order_id="TEST-CALL-WORKER-1", item_name="widget", status="shipped",
            delivery_date="soon", customer_name="Test Customer",
        )
        await order.insert()
        found = await Order.find_one(Order.order_id == "TEST-CALL-WORKER-1")

        await turn.delete()
        await order.delete()
        result_queue.put({"insert_ok": True, "order_found": found is not None})

    asyncio.run(run())


async def test_call_worker_child_process_can_write_to_the_database():
    result_queue: mp.Queue = mp.Queue()
    proc = mp.Process(target=_child_writes_to_db, args=(result_queue,), daemon=True)
    proc.start()
    result = result_queue.get(timeout=20)
    proc.join(timeout=5)

    assert proc.exitcode == 0, "child process should exit cleanly, not crash on a DB call"
    assert result["insert_ok"] is True
    assert result["order_found"] is True
