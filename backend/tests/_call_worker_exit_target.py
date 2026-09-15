"""Child-process entry points for test_call_worker_exits_after_the_call.py.

They live in their own importable module because 'spawn' re-imports the
target by name in a fresh interpreter. Each one runs the REAL
call_worker._handle_call with the WebRTC handler and the voice pipeline
replaced by fakes that reproduce what the real ones do to the process:
the pipeline starts a payment forwarder that waits on payment_queue through
run_in_executor, exactly like voice_pipeline._forward_payments, and then the
call ends.
"""

import asyncio
import multiprocessing as mp


class _FakeHandler:
    def __init__(self, ice_servers=None):
        pass

    async def handle_web_request(self, request, start_pipeline):
        asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(start_pipeline(object())))
        return {"sdp": "answer", "type": "answer", "pc_id": "pc-test"}

    async def handle_patch_request(self, patch):
        return None


def _patched_call_worker(shutdown_grace: float | None):
    from app.pipeline import call_worker

    call_worker.SmallWebRTCRequestHandler = _FakeHandler
    call_worker._build_ice_servers = lambda: []
    if shutdown_grace is not None:
        call_worker.SHUTDOWN_GRACE_SECONDS = shutdown_grace
    return call_worker


def run_one_call(ice_queue, payment_queue, answer_queue, extra_blocking_queue=None, shutdown_grace=None):
    call_worker = _patched_call_worker(shutdown_grace)

    async def fake_pipeline(webrtc_connection, payment_queue=None, extra=None, **_):
        loop = asyncio.get_running_loop()

        async def _forward_payments():  # voice_pipeline._forward_payments' shape
            while True:
                item = await loop.run_in_executor(None, payment_queue.get)
                if item is None:
                    return

        forwarder = asyncio.create_task(_forward_payments())
        if extra is not None:
            # Something else, unknown, also blocking a thread forever.
            asyncio.ensure_future(loop.run_in_executor(None, extra.get))
        await asyncio.sleep(0.3)  # the call happens
        forwarder.cancel()  # what on_client_disconnected does

    bot_config = {"extra": extra_blocking_queue}

    async def main():
        await call_worker._handle_call(
            fake_pipeline, bot_config, "offer", "offer", None, answer_queue, ice_queue, payment_queue
        )

    asyncio.run(main())


def is_child_process() -> bool:
    return mp.parent_process() is not None
