"""Review finding #5 (2026-09-11) — a leak in the gap after the try block.

connect() guards its capacity slot carefully, but the guard stopped one line
too early:

    try:
        ... claim a worker, wait for the SDP answer ...
    except BaseException:
        await release_call_slot(slot_token)
        raise

    _active_calls[answer["pc_id"]] = _ActiveCall(...)   <- unprotected

Everything in that last statement can fail. `answer["pc_id"]` is a KeyError
if the worker returned a malformed dict, and _ActiveCall construction can
raise on a bad value. At that point the slot has been claimed, the try/except
that would have released it has already closed, and nothing else in the
system knows this call ever existed — the reaper only walks _active_calls,
and the row was never added to it.

So the slot is held forever in the in-process backend, or until the ~1-hour
TTL sweep with Redis. Effective capacity shrinks silently, with nothing in
the logs pointing at the cause, until a restart.

The worker process leaks the same way and is the worse half: it is alive,
holding a media track, and unreferenced by anything that could ever reap it —
the same phantom-call shape as review finding C3 on the frontend.
"""

import pytest

from app.api import connect as connect_module
from app.core.call_capacity import (
    _InProcessCapacity,
    active_call_count,
    use_backend,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    use_backend(_InProcessCapacity())
    connect_module._active_calls.clear()
    monkeypatch.setattr(connect_module.settings, "max_concurrent_calls", 3)
    yield
    connect_module._active_calls.clear()
    use_backend(_InProcessCapacity())


class _FakeProcess:
    def __init__(self):
        self.terminated = False
        self.started = False
        self.pid = 999

    def start(self):
        self.started = True

    def is_alive(self):
        return not self.terminated

    def terminate(self):
        self.terminated = True

    def join(self, timeout=None):
        return None


def _arrange(monkeypatch, answer):
    """A connect() that gets all the way to registration, with a worker
    process we can inspect afterwards."""
    proc = _FakeProcess()

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
    monkeypatch.setattr(connect_module, "_idle_pool", [])

    class _Queue:
        def get(self, timeout=None):
            return answer

    def fake_process(*a, **k):
        return proc

    monkeypatch.setattr(connect_module._MP, "Queue", lambda: _Queue())
    monkeypatch.setattr(connect_module._MP, "Process", fake_process)
    return proc


class _FakeUser:
    id = "user-1"


async def test_a_malformed_answer_does_not_leak_the_capacity_slot(monkeypatch):
    """The worker came back with a dict that has no pc_id — a KeyError right
    where the old guard had just stopped protecting."""
    _arrange(monkeypatch, answer={"sdp": "x", "type": "answer"})  # no pc_id

    before = await active_call_count()
    body = connect_module.WebRTCOffer(bot_id="bot-1", sdp="x", type="offer")

    # KeyError specifically: `answer["pc_id"]` on a dict that has no
    # pc_id is the exact failure this guards, and asserting the precise
    # type keeps the test honest if the failure mode ever changes.
    with pytest.raises(KeyError):
        await connect_module.connect(body, current_user=_FakeUser())

    assert await active_call_count() == before, "the capacity slot leaked"


async def test_a_malformed_answer_does_not_leak_the_worker_process(monkeypatch):
    """The worse half: an unregistered process is alive, holding a media
    track, and invisible to the reaper, which only walks _active_calls."""
    proc = _arrange(monkeypatch, answer={"sdp": "x", "type": "answer"})

    body = connect_module.WebRTCOffer(bot_id="bot-1", sdp="x", type="offer")
    # KeyError specifically: `answer["pc_id"]` on a dict that has no
    # pc_id is the exact failure this guards, and asserting the precise
    # type keeps the test honest if the failure mode ever changes.
    with pytest.raises(KeyError):
        await connect_module.connect(body, current_user=_FakeUser())

    assert proc.terminated, "the worker process was left running and unreferenced"


async def test_a_successful_call_keeps_its_slot_and_its_process(monkeypatch):
    """The fix must not become over-eager — a call that registers correctly
    has to keep both."""
    proc = _arrange(monkeypatch, answer={"sdp": "x", "type": "answer", "pc_id": "pc-1"})

    body = connect_module.WebRTCOffer(bot_id="bot-1", sdp="x", type="offer")
    result = await connect_module.connect(body, current_user=_FakeUser())

    assert result["pc_id"] == "pc-1"
    assert "pc-1" in connect_module._active_calls
    assert not proc.terminated
    assert await active_call_count() == 1, "a live call should be holding its slot"
