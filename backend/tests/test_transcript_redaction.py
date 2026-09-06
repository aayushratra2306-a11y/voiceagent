"""Task 6.2 — the redaction rules from app/core/redaction.py actually reach
the database, not just the module's own tests.

TranscriptRecorder is where a completed turn is built and inserted; this
drives it with the real pipecat frames a live call produces and reads back
the ConversationTurn it wrote, which is the only way to prove redaction
happens BEFORE storage rather than being available but unused.
"""

import uuid

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallResultFrame,
    TextFrame,
    TranscriptionFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from app.models.conversation import ConversationTurn
from app.pipeline.voice_pipeline import TranscriptRecorder

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def unique_session_id() -> str:
    # A fresh id per test, so ConversationTurn.find_one below can never pick
    # up a document another test in this session already wrote.
    return str(uuid.uuid4())


async def _run_a_turn(recorder: TranscriptRecorder, user_says: str, bot_replies: str,
                       tool_result: dict | None = None) -> None:
    await recorder.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await recorder.process_frame(
        TranscriptionFrame(text=user_says, user_id="", timestamp=""),
        FrameDirection.DOWNSTREAM,
    )
    if tool_result is not None:
        await recorder.process_frame(
            FunctionCallResultFrame(
                function_name="charge_card", tool_call_id="t1", arguments={},
                result=tool_result,
            ),
            FrameDirection.DOWNSTREAM,
        )
    await recorder.process_frame(TextFrame(text=bot_replies), FrameDirection.DOWNSTREAM)
    await recorder.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await recorder.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)


async def _latest_turn(session_id: str) -> ConversationTurn:
    turn = await ConversationTurn.find_one(ConversationTurn.session_id == session_id)
    assert turn is not None, "TranscriptRecorder did not save a turn at all"
    return turn


async def test_a_spoken_card_number_never_reaches_the_database(unique_session_id):
    recorder = TranscriptRecorder(session_id=unique_session_id, bot_id="bot-1", bot_name="Auris")

    await _run_a_turn(
        recorder,
        user_says="my card is 4111 1111 1111 1111",
        bot_replies="Got it, processing that now.",
    )

    turn = await _latest_turn(unique_session_id)
    assert "4111" not in turn.user_transcript
    assert "[card number]" in turn.user_transcript
    assert "card" in turn.redacted_kinds


async def test_ordinary_conversation_is_stored_completely_unaltered(unique_session_id):
    recorder = TranscriptRecorder(session_id=unique_session_id, bot_id="bot-1", bot_name="Auris")

    await _run_a_turn(
        recorder,
        user_says="what time do you close today",
        bot_replies="We're open until 6pm today.",
    )

    turn = await _latest_turn(unique_session_id)
    assert turn.user_transcript == "what time do you close today"
    assert turn.assistant_reply == "We're open until 6pm today."
    assert turn.redacted_kinds == []


async def test_a_card_number_in_a_tool_calls_own_arguments_is_also_redacted(unique_session_id):
    """The other way a card number reaches the database: a customer's own
    payment tool carrying one in its result, stored on the turn exactly
    like the transcript is."""
    recorder = TranscriptRecorder(session_id=unique_session_id, bot_id="bot-1", bot_name="Auris")

    await _run_a_turn(
        recorder,
        user_says="please charge my card",
        bot_replies="Done, you're all set.",
        tool_result={"card_number": "4111111111111111", "status": "charged"},
    )

    turn = await _latest_turn(unique_session_id)
    stored_tool_calls = str(turn.tool_calls)
    assert "4111" not in stored_tool_calls
    assert turn.tool_calls[0]["result"]["status"] == "charged", "a non-sensitive field was altered"
    assert "card" in turn.redacted_kinds


async def test_a_bot_can_narrow_which_kinds_it_redacts(unique_session_id):
    """The manual's own requirement: configurable per customer."""
    recorder = TranscriptRecorder(
        session_id=unique_session_id, bot_id="bot-1", bot_name="Auris",
        redaction_kinds=["phone"],
    )

    await _run_a_turn(
        recorder,
        user_says="my card is 4111 1111 1111 1111",
        bot_replies="ok",
    )

    turn = await _latest_turn(unique_session_id)
    assert "4111" in turn.user_transcript, "a bot narrowed to 'phone' still redacted a card number"


async def test_no_bot_configuration_defaults_to_full_redaction(unique_session_id):
    """None must mean 'not configured, or explicitly cleared' resolving to
    the SAFE answer — not an empty, do-nothing redaction set. This is what
    stops an older bot_config (from before this field existed) or a bug
    that clears the list from silently storing card numbers in plaintext."""
    recorder = TranscriptRecorder(
        session_id=unique_session_id, bot_id="bot-1", bot_name="Auris",
        redaction_kinds=None,
    )

    await _run_a_turn(
        recorder,
        user_says="my card is 4111 1111 1111 1111",
        bot_replies="ok",
    )

    turn = await _latest_turn(unique_session_id)
    assert "4111" not in turn.user_transcript


async def test_an_explicitly_empty_list_means_no_redaction_at_all(unique_session_id):
    """The one deliberate exception to "None means safe": an operator who
    explicitly sets redact_transcripts=[] (turning it off on purpose,
    which the API allows) gets exactly that, not silently overridden back
    to full redaction."""
    recorder = TranscriptRecorder(
        session_id=unique_session_id, bot_id="bot-1", bot_name="Auris",
        redaction_kinds=[],
    )

    await _run_a_turn(
        recorder,
        user_says="my card is 4111 1111 1111 1111",
        bot_replies="ok",
    )

    turn = await _latest_turn(unique_session_id)
    assert "4111" in turn.user_transcript


async def test_the_assistant_side_is_redacted_too(unique_session_id):
    """A bot echoing back a number it just heard is exactly as much of a
    liability as the caller saying it in the first place."""
    recorder = TranscriptRecorder(session_id=unique_session_id, bot_id="bot-1", bot_name="Auris")

    await _run_a_turn(
        recorder,
        user_says="can you confirm my number",
        bot_replies="Sure, I have 9876543210 on file for you.",
    )

    turn = await _latest_turn(unique_session_id)
    assert "9876543210" not in turn.assistant_reply
    assert "phone" in turn.redacted_kinds
