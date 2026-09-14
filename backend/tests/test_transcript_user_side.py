"""Found 2026-09-14: the caller's side of every conversation was never saved.

Every turn in production had user_transcript == "" (all 145 turns in the
2026-09-13 backup, 07-13 Sep; all 146 in the pre-incident oplog). The bot's
replies were saved.

TranscriptRecorder filled user_transcript from TranscriptionFrame, assuming
those frames flow downstream past the user aggregator. They do not: pipecat
1.7's LLMUserAggregator consumes them ("consumed here and not pushed
downstream", llm_response_universal.py) and delivers the finished turn's
text through its on_user_turn_stopped event instead. The recorder sits after
the aggregator, so it never saw a single word. Same pipecat version since the
recorder was written (2026-08-31): this never worked, and no test ran the
recorder behind a real aggregator. These tests use the REAL aggregator.

No database is touched: ConversationTurn.insert is replaced by a recorder.
"""

import asyncio
import inspect

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TextFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMUserAggregator,
    LLMUserAggregatorParams,
)
from pipecat.tests.utils import SleepFrame, run_test

from app.models.conversation import ConversationTurn
from app.pipeline import voice_pipeline
from app.pipeline.voice_pipeline import TranscriptRecorder, record_user_turns

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def saved(monkeypatch):
    turns: list[ConversationTurn] = []

    async def fake_insert(self, *args, **kwargs):
        turns.append(self)
        return self

    monkeypatch.setattr(ConversationTurn, "insert", fake_insert)
    return turns


def _caller_says(text: str) -> list:
    return [
        VADUserStartedSpeakingFrame(),
        TranscriptionFrame(text=text, user_id="caller", timestamp="now"),
        SleepFrame(sleep=0.1),
        VADUserStoppedSpeakingFrame(),
        SleepFrame(sleep=1.2),  # the aggregator's turn-stop strategies settle
    ]


def _bot_says(text: str) -> list:
    # The pause matters: BotStoppedSpeakingFrame is a system frame, handled
    # ahead of queued data frames, and in a real call it arrives only once the
    # audio for the text has finished playing, seconds later.
    return [
        BotStartedSpeakingFrame(),
        TextFrame(text=text),
        SleepFrame(sleep=0.1),
        BotStoppedSpeakingFrame(),
        SleepFrame(sleep=0.1),
    ]


def _real_aggregator() -> LLMUserAggregator:
    """pipecat's real user aggregator. Its turn-stop watchdog is shortened
    from 5.0s: production ends turns with the Smart Turn model, which judges
    from audio, and these tests send no audio, so only the watchdog can end a
    turn here (measured: with the default, no turn ended within 4s)."""
    return LLMUserAggregator(LLMContext(), params=LLMUserAggregatorParams(user_turn_stop_timeout=0.4))


def _pipeline():
    aggregator = _real_aggregator()
    recorder = TranscriptRecorder(session_id="s-1", bot_id="bot-1", bot_name="Nitya")
    record_user_turns(aggregator, recorder)
    return Pipeline([aggregator, recorder])


# --- the root cause, pinned down --------------------------------------------------

async def test_the_real_aggregator_keeps_the_callers_words_to_itself():
    """Why the recorder never saw anything. If a pipecat upgrade changes this,
    this test says so before the recorder starts saving words twice."""
    aggregator = _real_aggregator()
    delivered = []

    @aggregator.event_handler("on_user_turn_stopped")
    async def on_stopped(_aggregator, _strategy, message):
        delivered.append(message.content)

    down, _up = await run_test(aggregator, frames_to_send=_caller_says("what is on page fifty"))

    assert not any(isinstance(f, TranscriptionFrame) for f in down)
    assert delivered == ["what is on page fifty"]


# --- the fix, through the real aggregator -------------------------------------------

async def test_the_callers_words_are_saved_with_the_reply(saved):
    frames = _caller_says("what is on page fifty") + _bot_says("Page 50 covers context windows.")

    await run_test(_pipeline(), frames_to_send=frames)

    replies = [t for t in saved if t.assistant_reply]
    assert replies, "no turn was saved"
    assert replies[-1].user_transcript == "what is on page fifty"
    assert replies[-1].assistant_reply == "Page 50 covers context windows."


async def test_the_greeting_is_saved_without_caller_words(saved):
    frames = _bot_says("Hello! How can I help?") + _caller_says("hi") + _bot_says("Hi there.")

    await run_test(_pipeline(), frames_to_send=frames)

    assert [(t.user_transcript, t.assistant_reply) for t in saved] == [
        ("", "Hello! How can I help?"),
        ("hi", "Hi there."),
    ]


async def test_everything_said_before_the_bot_replies_is_kept(saved):
    """Live 2026-09-13: three sentences, pauses between, one reply. Keeping only
    the last would save "The headline of that particular page." on its own."""
    frames = (
        _caller_says("I have uploaded a PDF.")
        + _caller_says("What is on page fifty?")
        + _bot_says("Page 50 covers context windows.")
    )

    await run_test(_pipeline(), frames_to_send=frames)

    assert saved[-1].user_transcript == "I have uploaded a PDF. What is on page fifty?"


async def test_the_callers_last_words_are_saved_when_the_call_ends(saved):
    """Seen twice in the 2026-09-14 logs: "That's all for now. Thank you."
    then the caller hung up before any reply, and it was never saved."""
    frames = _bot_says("Anything else?") + _caller_says("That's all for now. Thank you.")

    await run_test(_pipeline(), frames_to_send=frames)  # run_test ends with an EndFrame

    assert saved[-1].user_transcript == "That's all for now. Thank you."
    assert saved[-1].assistant_reply == ""


async def test_a_call_that_ends_with_nothing_pending_saves_nothing_extra(saved):
    await run_test(_pipeline(), frames_to_send=_bot_says("Goodbye."))

    assert len(saved) == 1


def test_a_blank_turn_is_not_recorded():
    recorder = TranscriptRecorder(session_id="s", bot_id=None, bot_name="")
    recorder.add_user_text("   ")
    recorder.add_user_text(None)
    assert recorder._user_parts == []


def test_the_live_pipeline_wires_the_recorder_to_the_turn_event():
    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "record_user_turns(user_aggregator, transcript_recorder)" in source
    assert "transcript_recorder,\n" in source, "the wired recorder must be the one placed in the pipeline"


async def test_a_saving_failure_on_hang_up_does_not_block_the_call_ending(monkeypatch):
    async def hanging_insert(self, *args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(ConversationTurn, "insert", hanging_insert)
    monkeypatch.setattr(voice_pipeline, "FINAL_TURN_SAVE_TIMEOUT_SECONDS", 0.2)

    started = asyncio.get_running_loop().time()
    await run_test(_pipeline(), frames_to_send=_caller_says("bye"))

    assert asyncio.get_running_loop().time() - started < 5, "ending the call waited on a stuck database write"


async def test_words_cut_off_by_hanging_up_mid_sentence_are_saved(saved):
    """The caller hangs up before the turn is judged finished. The aggregator
    only releases that text as the call ends, in the same moment the recorder
    is saving the last turn."""
    aggregator = LLMUserAggregator(LLMContext())  # default 5s watchdog: the turn never ends on its own
    recorder = TranscriptRecorder(session_id="s-1", bot_id="bot-1", bot_name="Nitya")
    record_user_turns(aggregator, recorder)
    frames = [
        VADUserStartedSpeakingFrame(),
        TranscriptionFrame(text="and one more thing about page", user_id="caller", timestamp="now"),
        SleepFrame(sleep=0.2),
    ]

    await run_test(Pipeline([aggregator, recorder]), frames_to_send=frames)

    assert saved and saved[-1].user_transcript == "and one more thing about page"


async def test_words_spoken_while_the_previous_turn_is_saving_are_kept(monkeypatch):
    """Independent review, 2026-09-14. The caller cuts the bot off with a short
    "no, stop": the bot stops, its turn starts saving (a round trip to Atlas),
    and the caller's turn finishes while that save is still in flight. The
    pending turn used to be cleared only after the save returned, wiping the
    words that had just arrived."""
    turns: list[ConversationTurn] = []

    async def slow_insert(self, *args, **kwargs):
        await asyncio.sleep(1.0)
        turns.append(self)
        return self

    monkeypatch.setattr(ConversationTurn, "insert", slow_insert)
    frames = (
        _bot_says("Page 50 covers context windows and")  # BotStopped -> 1s save begins
        + _caller_says("no, stop")                      # turn ends ~0.5s into that save
        + [SleepFrame(sleep=0.8)]
        + _bot_says("Sure, stopping.")
        + [SleepFrame(sleep=1.2)]
    )

    await run_test(_pipeline(), frames_to_send=frames)

    assert ("no, stop", "Sure, stopping.") in [(t.user_transcript, t.assistant_reply) for t in turns], (
        [(t.user_transcript, t.assistant_reply) for t in turns]
    )
