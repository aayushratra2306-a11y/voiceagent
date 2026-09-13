"""Task 4.3 — the autoscaler could not see the callers it existed for.

Found live 2026-09-13, running 4.3's own acceptance test ("a burst of calls
visibly triggers new workers"). It never did. The logs had the reason:

    06:46:14  [POOL] Claimed still starting worker pid=10731 (1 left)
    06:46:36  [CALL] Started call worker pid=10731 ...

A caller was handed a worker that was still importing the pipeline stack
and waited 22 seconds for their call to start — the exact cold start the
warm pool exists to prevent. And the autoscaler did not register anything,
because its only demand signal was the pool LIST being empty:

    try:
        worker = _idle_pool.pop(0)
    except IndexError:
        worker = None          # <- only this path set the exhaustion flag

A claimed worker is replaced immediately, and the replacement goes into the
list the moment its process starts — about 8 seconds before it can take a
call. So under a real burst the list is almost never empty. Callers get
still-starting workers, pay the full cold start, and the pool reports
"1 left" every time and never grows.

The right question was never "is the list empty" but "did this caller get a
worker that was ready". Those differ for exactly the window that matters.
"""

import pytest

from app.api import connect as connect_module
from app.core.call_capacity import _InProcessCapacity, use_backend

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _Ready:
    def __init__(self, is_set: bool):
        self._set = is_set

    def is_set(self):
        return self._set


class _Proc:
    pid = 4242

    def is_alive(self):
        return True

    def terminate(self):
        pass

    def join(self, timeout=None):
        pass


class _Queue:
    def __init__(self, answer=None):
        self._answer = answer

    def put(self, item):
        pass

    def get(self, timeout=None):
        return self._answer


def _worker(ready: bool):
    return connect_module._PooledWorker(
        process=_Proc(),
        job_queue=_Queue(),
        answer_queue=_Queue({"sdp": "x", "type": "answer", "pc_id": f"pc-{ready}"}),
        ice_queue=_Queue(),
        ready=_Ready(ready),
        spawned_at=0.0,
        payment_queue=_Queue(),
    )


class _User:
    id = "user-1"


@pytest.fixture(autouse=True)
def _arrange(monkeypatch):
    use_backend(_InProcessCapacity())
    connect_module._active_calls.clear()
    monkeypatch.setattr(connect_module.settings, "max_concurrent_calls", 10)
    monkeypatch.setattr(connect_module, "_pool_exhausted_since_last_check", False)

    class _Bot:
        id = "bot-1"
        name = "Test"
        system_prompt = "p"
        voice_id = "v"
        llm_model = "m"
        language = "en"
        user_id = "user-1"

    async def owned(*a, **k):
        return _Bot()

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(connect_module, "fetch_owned_bot", owned)
    monkeypatch.setattr(connect_module, "_end_previous_calls_for", noop)
    # A real top-up spawns real interpreters.
    monkeypatch.setattr(connect_module, "_top_up_pool", lambda: None)
    yield
    connect_module._active_calls.clear()
    use_backend(_InProcessCapacity())


async def _call():
    body = connect_module.WebRTCOffer(bot_id="bot-1", sdp="x", type="offer")
    return await connect_module.connect(body, _User())


async def test_a_caller_handed_a_still_starting_worker_counts_as_demand(monkeypatch):
    """The 06:46:14 log line. The list was not empty, so nothing was
    recorded — while the caller waited 22 seconds."""
    monkeypatch.setattr(connect_module, "_idle_pool", [_worker(ready=False)])

    await _call()

    assert connect_module._pool_exhausted_since_last_check is True, (
        "a caller paid a cold start and the autoscaler was not told"
    )


async def test_a_caller_handed_a_warm_worker_is_not_demand(monkeypatch):
    """The pool doing its job. Counting this would grow the pool on every
    single call and pin it at the maximum for no reason."""
    monkeypatch.setattr(connect_module, "_idle_pool", [_worker(ready=True)])

    await _call()

    assert connect_module._pool_exhausted_since_last_check is False


async def test_an_empty_pool_still_counts_as_demand(monkeypatch):
    """The original signal must survive the change."""
    monkeypatch.setattr(connect_module, "_idle_pool", [])
    monkeypatch.setattr(connect_module._MP, "Queue", lambda: _Queue({"sdp": "x", "type": "answer", "pc_id": "pc-cold"}))

    class _ColdProc(_Proc):
        def start(self):
            pass

    monkeypatch.setattr(connect_module._MP, "Process", lambda *a, **k: _ColdProc())

    await _call()

    assert connect_module._pool_exhausted_since_last_check is True


async def test_the_logged_burst_would_now_grow_the_pool():
    """End to end through the real decision function: the flag this sets is
    exactly the input next_pool_target() grows on."""
    target = connect_module.next_pool_target(
        current_target=2, exhausted=True, quiet_ticks=0,
        available_memory_mb=2000, pool_min=2, pool_max=4, min_free_memory_mb=700,
    )
    assert target == 3
