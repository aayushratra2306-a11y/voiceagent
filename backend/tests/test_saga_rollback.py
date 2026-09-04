"""Task 3.4 — undoing earlier steps when a later one fails.

The manual calls this the step almost everyone skips, and says why: calling
three APIs in a row is easy, correctly undoing the first two when the third
fails is not, and without it a half-finished workflow leaves real charges on
a real customer's card.

The rule these tests pin above all others is what decides whether something
gets rolled back: only a tool that DECLARES how to undo itself ever is.
That keeps two lookups from "rolling back" (there is nothing to roll back)
while a booking beside a failed payment does get cancelled — the intent
lives in the configuration, not in a guess made here.

And the manual's warning, which several of these cover: some things
genuinely cannot be undone. Nothing may pretend otherwise. A tool with no
undo is reported as still standing, a failed undo is escalated rather than
glossed over, and the caller is told precisely what holds and what does not.
"""

import pytest

from app.models.bot_tool import BotTool, ToolUndo
from app.pipeline.saga import SAGA_RULE, TurnSaga
from app.services import tool_registry

pytestmark = pytest.mark.asyncio(loop_scope="session")

OK = {"ok": True, "data": {"id": "BK-1"}}
BAD = {"ok": False, "error": "http_500"}


def _tool(name, undo_url="") -> BotTool:
    return BotTool(
        bot_id="b", name=name, description="d",
        method="POST", url=f"https://api.test/{name}",
        undo=ToolUndo(url=undo_url, method="DELETE") if undo_url else ToolUndo(),
    )


class _Recorder:
    """Captures the undo calls the saga makes."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def __call__(self, tool, args):
        self.calls.append((tool.name, tool.method, tool.url, args))
        return {"ok": not self.fail, "error": None if not self.fail else "http_409"}


def _patch_calls(monkeypatch, recorder):
    monkeypatch.setattr(tool_registry, "call_http_tool", recorder)


# --- what gets rolled back, and what does not ------------------------------

async def test_a_success_beside_a_failure_is_undone(monkeypatch):
    """The case the whole task exists for."""
    rec = _Recorder()
    _patch_calls(monkeypatch, rec)
    said = []
    saga = TurnSaga(announce=lambda s: said.append(s) or _noop())

    saga.begin(2)
    await saga.record("book_cab", _tool("book_cab", "https://api.test/cancel/{ref}"), {"ref": "R1"}, OK)
    await saga.record("charge_card", _tool("charge_card"), {}, BAD)

    assert [c[0] for c in rec.calls] == ["undo_book_cab"], rec.calls
    assert rec.calls[0][1] == "DELETE"
    assert said, "the caller was told nothing about a half-finished request"


async def test_a_tool_with_no_declared_undo_is_left_alone(monkeypatch):
    """Two lookups where one fails must not "roll back" the other — there is
    nothing to roll back, and inventing one would be worse than doing
    nothing."""
    rec = _Recorder()
    _patch_calls(monkeypatch, rec)
    said = []
    saga = TurnSaga(announce=lambda s: said.append(s) or _noop())

    saga.begin(2)
    await saga.record("check_stock", _tool("check_stock"), {}, OK)
    await saga.record("check_price", _tool("check_price"), {}, BAD)

    assert rec.calls == [], "undid something that never declared how to be undone"
    # It still has to be mentioned: it succeeded and it stands.
    assert "still stand" in said[0], said


async def test_nothing_is_rolled_back_when_everything_worked(monkeypatch):
    rec = _Recorder()
    _patch_calls(monkeypatch, rec)
    said = []
    saga = TurnSaga(announce=lambda s: said.append(s) or _noop())

    saga.begin(2)
    await saga.record("book_cab", _tool("book_cab", "https://api.test/cancel"), {}, OK)
    await saga.record("send_sms", _tool("send_sms"), {}, OK)

    assert rec.calls == []
    assert said == [], "announced a problem on a turn where nothing went wrong"


async def test_steps_are_undone_in_reverse_order(monkeypatch):
    """Later steps may depend on earlier ones — the hotel booked against the
    flight has to go before the flight does."""
    rec = _Recorder()
    _patch_calls(monkeypatch, rec)
    saga = TurnSaga(announce=lambda s: _noop())

    saga.begin(3)
    await saga.record("book_flight", _tool("book_flight", "https://api.test/f/cancel"), {}, OK)
    await saga.record("book_hotel", _tool("book_hotel", "https://api.test/h/cancel"), {}, OK)
    await saga.record("send_itinerary", _tool("send_itinerary"), {}, BAD)

    assert [c[0] for c in rec.calls] == ["undo_book_hotel", "undo_book_flight"], rec.calls


async def test_the_undo_call_sees_the_original_arguments(monkeypatch):
    """So a cancel URL can be written /bookings/{booking_id} using the same
    placeholders the booking used."""
    rec = _Recorder()
    _patch_calls(monkeypatch, rec)
    saga = TurnSaga(announce=lambda s: _noop())

    saga.begin(2)
    await saga.record("book", _tool("book", "https://api.test/cancel/{booking_id}"),
                      {"booking_id": "BK-77"}, OK)
    await saga.record("pay", _tool("pay"), {}, BAD)

    assert rec.calls[0][3] == {"booking_id": "BK-77"}


async def test_the_undo_carries_the_same_credential():
    """A cancel endpoint needs the authorisation the booking had."""
    from app.core.crypto import encrypt_secret
    from app.models.bot_tool import ToolAuth

    tool = _tool("book", "https://api.test/cancel")
    tool.auth = ToolAuth(kind="bearer", secret_encrypted=encrypt_secret("k"))
    assert tool.as_undo_tool().auth.secret_encrypted == tool.auth.secret_encrypted


# --- the manual's warning: some things cannot be undone --------------------

async def test_a_failed_undo_is_escalated_not_glossed_over(monkeypatch):
    """The one case here that genuinely needs a human: something real
    happened and could not be taken back."""
    rec = _Recorder(fail=True)
    _patch_calls(monkeypatch, rec)
    errors, said = [], []
    monkeypatch.setattr("app.pipeline.saga.logger.error", lambda m: errors.append(m))
    saga = TurnSaga(announce=lambda s: said.append(s) or _noop())

    saga.begin(2)
    await saga.record("book_cab", _tool("book_cab", "https://api.test/cancel"), {"ref": "R9"}, OK)
    await saga.record("send_sms", _tool("send_sms"), {}, BAD)

    assert errors, "a failed undo was not raised to error level"
    assert "COULD NOT UNDO" in errors[0] and "R9" in errors[0], errors
    assert "do not promise they are cancelled" in said[0].lower(), said


async def test_the_caller_is_told_what_still_stands(monkeypatch):
    """The manual is explicit: do not pretend. A sent message stays sent."""
    rec = _Recorder()
    _patch_calls(monkeypatch, rec)
    said = []
    saga = TurnSaga(announce=lambda s: said.append(s) or _noop())

    saga.begin(3)
    await saga.record("send_sms", _tool("send_sms"), {}, OK)                       # irreversible
    await saga.record("book_cab", _tool("book_cab", "https://api.test/c"), {}, OK)  # reversible
    await saga.record("charge_card", _tool("charge_card"), {}, BAD)

    sentence = said[0]
    assert "send_sms" in sentence and "CANNOT be undone" in sentence
    assert "book_cab" in sentence and "undone automatically" in sentence
    assert "charge_card" in sentence
    assert "Do not describe anything as done unless it is listed as standing" in sentence


async def test_a_rollback_never_raises_into_a_live_call(monkeypatch):
    """This runs while someone is on the line. An exception here would
    replace a bad-news sentence with silence."""
    async def explode(tool, args):
        raise RuntimeError("undo endpoint is gone")

    monkeypatch.setattr(tool_registry, "call_http_tool", explode)
    said = []
    saga = TurnSaga(announce=lambda s: said.append(s) or _noop())

    saga.begin(2)
    await saga.record("book", _tool("book", "https://api.test/cancel"), {}, OK)
    await saga.record("pay", _tool("pay"), {}, BAD)

    assert said, "an exception during rollback left the caller with nothing"
    assert "could not be undone" in said[0].lower()


# --- batch boundaries ------------------------------------------------------

async def test_a_new_turn_cannot_roll_back_the_previous_one(monkeypatch):
    """Undoing a booking made two turns ago because something unrelated
    failed now would be its own kind of disaster."""
    rec = _Recorder()
    _patch_calls(monkeypatch, rec)
    saga = TurnSaga(announce=lambda s: _noop())

    saga.begin(1)
    await saga.record("book_cab", _tool("book_cab", "https://api.test/cancel"), {}, OK)

    saga.begin(1)                                   # a new turn
    await saga.record("check_stock", _tool("check_stock"), {}, BAD)

    assert rec.calls == [], "rolled back a step from an earlier turn"


async def test_an_incomplete_batch_does_nothing(monkeypatch):
    """If one tool never reports, the saga stays idle rather than acting on
    half a picture or hanging the turn."""
    rec = _Recorder()
    _patch_calls(monkeypatch, rec)
    saga = TurnSaga(announce=lambda s: _noop())

    saga.begin(3)
    await saga.record("book", _tool("book", "https://api.test/cancel"), {}, OK)
    await saga.record("pay", _tool("pay"), {}, BAD)
    # the third result never arrives
    assert rec.calls == []


async def test_a_batch_is_acted_on_only_once(monkeypatch):
    rec = _Recorder()
    _patch_calls(monkeypatch, rec)
    saga = TurnSaga(announce=lambda s: _noop())

    saga.begin(2)
    await saga.record("book", _tool("book", "https://api.test/cancel"), {}, OK)
    await saga.record("pay", _tool("pay"), {}, BAD)
    await saga.record("stray", _tool("stray"), {}, BAD)   # a late arrival

    assert len(rec.calls) == 1, "rolled the same step back twice"


# --- wiring ----------------------------------------------------------------

def test_the_rule_is_only_added_for_bots_with_a_reversible_tool():
    import inspect

    from app.pipeline import voice_pipeline

    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "if has_undo:" in source
    assert "SAGA_RULE" in source


def test_the_saga_is_told_how_many_results_to_expect():
    """Pipecat announces a batch starting but never that it finished, so
    without this the saga could never know a batch was complete."""
    import inspect

    from app.pipeline import voice_pipeline

    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "saga.begin(len(function_calls))" in source


def test_the_model_is_told_not_to_round_the_outcome():
    rule = SAGA_RULE.lower()
    assert "never say everything worked" in rule
    assert "never say everything failed" in rule


async def _noop():
    return None
