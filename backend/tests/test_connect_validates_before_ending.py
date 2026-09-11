"""Review finding #4 (2026-09-11) — one bad request destroyed a good call.

`connect()` enforces one live call per user by killing any earlier one. That
rule is right and stays (see `_end_previous_calls_for`'s docstring for the
"caller heard two bots at once" incident it exists to prevent).

What was wrong was WHEN it ran. The order was:

    1. _end_previous_calls_for(user)   <- tears down their live call
    2. try_acquire_call_slot()
    3. fetch_owned_bot(body.bot_id)    <- first time the request is validated

So a request naming a bot_id that does not exist, or belongs to somebody
else, killed the caller's existing healthy call at step 1 and only then
failed at step 3. The caller was left with no call at all, having had a
working one a moment earlier — a stale browser tab, a bookmarked id, or a
retry against a deleted bot was enough to do it.

Nothing about the one-call-per-user rule requires acting before the request
is known to be valid. The new order validates first:

    1. fetch_owned_bot(body.bot_id)    <- 404/403 here costs the caller nothing
    2. _end_previous_calls_for(user)   <- frees their own slot
    3. try_acquire_call_slot()

Step 2 still precedes step 3, which is the part that actually mattered about
the original ordering: a user reconnecting must never be refused by their own
stale call's capacity slot.

The cost is one indexed Mongo lookup before the capacity check, where the old
comment deliberately avoided it. That trade is worth making explicit: being
refused at capacity is transient and harmless, whereas destroying a live
conversation is neither, and paying an indexed lookup to tell a caller their
request was invalid is the normal price of validating a request at all.
"""

import pytest

from app.api import connect as connect_module

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeProcess:
    def __init__(self):
        self.terminated = False
        self.pid = 4242

    def is_alive(self):
        return not self.terminated

    def terminate(self):
        self.terminated = True

    def join(self, timeout=None):
        return None


@pytest.fixture
def live_call(monkeypatch):
    """A healthy call already in progress for this user."""
    connect_module._active_calls.clear()
    proc = _FakeProcess()
    call = connect_module._ActiveCall(
        proc, None, "user-1", None, "slot-token-for-the-live-call"
    )
    connect_module._active_calls["existing-pc-id"] = call
    yield proc
    connect_module._active_calls.clear()


async def test_an_unowned_bot_id_does_not_kill_the_callers_existing_call(
    live_call, monkeypatch, client
):
    """The bug, stated directly: a request that is about to be rejected must
    not tear anything down on its way to being rejected."""
    from fastapi import HTTPException

    async def reject(bot_id, user):
        raise HTTPException(status_code=404, detail="Bot not found")

    monkeypatch.setattr(connect_module, "fetch_owned_bot", reject)

    released = []
    monkeypatch.setattr(
        connect_module, "release_call_slot",
        lambda token: released.append(token) or _noop(),
    )

    class _User:
        id = "user-1"

    with pytest.raises(HTTPException) as raised:
        await connect_module.connect(
            connect_module.WebRTCOffer(bot_id="does-not-exist", sdp="x", type="offer"),
            current_user=_User(),
        )

    assert raised.value.status_code == 404
    assert not live_call.terminated, "the caller's working call was destroyed by a bad request"
    assert "existing-pc-id" in connect_module._active_calls
    assert released == [], "the live call's capacity slot was released"


async def test_a_valid_request_still_ends_the_previous_call(live_call, monkeypatch):
    """The one-call-per-user rule has to keep working — this must not become
    a way to run two pipelines for one caller."""
    ended = []

    async def accept(bot_id, user):
        class _Bot:
            id = "bot-1"
            name = "Test"
            system_prompt = "p"
            voice_id = "v"
            llm_model = "m"
            language = "en"
            user_id = "user-1"
        return _Bot()

    async def fake_end(user_id):
        ended.append(user_id)

    monkeypatch.setattr(connect_module, "fetch_owned_bot", accept)
    monkeypatch.setattr(connect_module, "_end_previous_calls_for", fake_end)
    # Stop before the real worker machinery — the ordering is what is under
    # test, not the WebRTC handshake.
    monkeypatch.setattr(
        connect_module, "try_acquire_call_slot", _raise_after_ordering_check(ended)
    )

    class _User:
        id = "user-1"

    with pytest.raises(_OrderingReached):
        await connect_module.connect(
            connect_module.WebRTCOffer(bot_id="bot-1", sdp="x", type="offer"),
            current_user=_User(),
        )

    assert ended == ["user-1"], "the previous call was not ended for a valid request"


class _OrderingReached(Exception):
    """Marker: execution got as far as the capacity check."""


def _raise_after_ordering_check(ended):
    async def _acquire():
        # Asserted here rather than afterwards so the ordering itself is what
        # fails the test, not a later symptom of it.
        assert ended, "capacity was claimed before the previous call was ended"
        raise _OrderingReached()
    return _acquire


async def _noop():
    return None
