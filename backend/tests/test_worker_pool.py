"""Latency (2026-09-03) — the pre-warmed worker pool.

The pool exists because a fresh interpreter needs 6.8s warm (13.7s cold) to
import the pipeline stack and connect to MongoDB, and before pooling the
caller waited through all of it after pressing Start.

The property that matters is: a pooled worker completes that startup and
signals ready WITHOUT having been given a call. If that ever stops being
true the pool still "works" -- calls are handled, nothing errors -- while
silently delivering none of the benefit, because every worker would do its
importing after the job arrives instead of before. That is exactly the kind
of regression no test catches unless it asserts on the ordering.

Uses the real spawn context, for the same reason test_call_worker does: the
whole design is about process boundaries, and fork would not exercise it.
"""

import multiprocessing as mp
import os
import time

import pytest

os.environ.setdefault("DB_NAME", "voiceagent_test")

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Generous: this genuinely does import pipecat and reach Atlas. The assertion
# is about ordering, not speed -- a slow CI box should not fail the suite.
READY_TIMEOUT_SECONDS = 120


async def test_pooled_worker_warms_up_before_any_call_arrives():
    from app.pipeline.call_worker import pooled_worker_main

    ctx = mp.get_context("spawn")
    job_queue = ctx.Queue()
    answer_queue = ctx.Queue()
    ice_queue = ctx.Queue()
    ready = ctx.Event()

    proc = ctx.Process(
        target=pooled_worker_main,
        args=(job_queue, answer_queue, ice_queue, ready),
        daemon=True,
    )
    started = time.perf_counter()
    proc.start()

    try:
        # The point of the whole exercise: it becomes ready having been sent
        # NOTHING. No job was ever put on job_queue.
        assert ready.wait(timeout=READY_TIMEOUT_SECONDS), (
            "pooled worker never signalled ready — it is doing its startup "
            "after a job arrives, which is the cost pooling exists to remove"
        )
        warmup = time.perf_counter() - started
        assert proc.is_alive(), "worker exited instead of waiting for a call"
        assert job_queue.empty(), "nothing should have been consumed yet"
        print(f"\n  pooled worker warm in {warmup:.2f}s, idle and waiting")

        # Sentinel shuts it down cleanly, which is how the pool retires a
        # worker it no longer needs.
        job_queue.put(None)
        proc.join(timeout=30)
        assert proc.exitcode == 0, f"expected clean exit, got {proc.exitcode}"
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
