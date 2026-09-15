"""Found 2026-09-15 while verifying Task 4.7: a finished call's worker
process never exited, so its capacity slot was never given back.

Live evidence: a test call hung up at 15:24:42; two minutes later its worker
(container pid 20) was still running, idle, with two threads blocked reading
a pipe, and Redis still held the call's slot. The API frees a slot only when
the call's process has exited (connect.py reap_dead_calls_loop), so every
call held one of the 6 places for the 65-minute TTL: six calls within an
hour and the next caller is refused "at capacity" with nobody on the phone.
The Watchdog also reads that count, so it would keep deferring restarts for
calls that do not exist. And each lingering worker keeps its memory.

Cause: call_worker._forward_ice and voice_pipeline._forward_payments each
wait for the next message with `run_in_executor(None, queue.get)`. When the
call ends the tasks are cancelled, but a thread blocked in queue.get() cannot
be. asyncio.run then shuts down the default executor, which in Python 3.11
waits for those threads without a timeout, and nothing ever sends the `None`
stop message both forwarders are written to wait for. Reproduced standalone
first: the process stayed alive after its call ended and exited the moment
one message was put on the queue.

These tests start real spawned processes; no network, no database.
"""

import multiprocessing as mp
import time

from app.pipeline import call_worker
from tests import _call_worker_exit_target as target

CTX = mp.get_context("spawn")


def _start(**kwargs):
    ice_queue, payment_queue, answer_queue = CTX.Queue(), CTX.Queue(), CTX.Queue()
    process = CTX.Process(
        target=target.run_one_call, args=(ice_queue, payment_queue, answer_queue), kwargs=kwargs
    )
    process.start()
    return process, answer_queue


def _exits_within(process, seconds: float) -> bool:
    process.join(seconds)
    alive = process.is_alive()
    if alive:
        process.kill()
        process.join(5)
    return not alive


def test_a_worker_process_exits_once_its_call_has_ended():
    process, answer_queue = _start()
    assert answer_queue.get(timeout=60)["pc_id"] == "pc-test", "the call never started"

    # Startup in a fresh interpreter takes a few seconds on its own; the call
    # itself lasts 0.3s. Well inside this, a fixed worker has exited.
    assert _exits_within(process, 25), (
        "the worker was still running after its call ended: its capacity slot is never released"
    )


def test_an_unknown_blocked_thread_cannot_keep_a_finished_call_alive():
    """The safety net: something not yet known also blocks a thread forever.
    A finished call's process must still end, shortly after the grace."""
    blocker = CTX.Queue()
    process, answer_queue = _start(extra_blocking_queue=blocker, shutdown_grace=2.0)
    assert answer_queue.get(timeout=60)["pc_id"] == "pc-test"

    started = time.monotonic()
    assert _exits_within(process, 25), "a blocked thread kept the finished call's process alive"
    assert time.monotonic() - started < 25


def test_the_safety_net_is_generous_by_default():
    """Long enough for a normal shutdown (final transcript save, cleanup) to
    finish on its own; it exists for the stuck case, not the usual one."""
    assert 10 <= call_worker.SHUTDOWN_GRACE_SECONDS <= 120


def test_the_safety_net_never_arms_in_the_main_process():
    """It ends the process with os._exit. In the API server or a test runner
    that would be a disaster, so it only ever arms inside a spawned worker."""
    assert not target.is_child_process()
    assert call_worker._arm_exit_after_call(grace=0.01) is False
