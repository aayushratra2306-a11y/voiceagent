"""Task 6.3 — the consent announcement, its audit log, and the scheduled
job that deletes transcripts past their retention period.

Three separate concerns, tested against real behaviour rather than mocks
wherever the point is proving something actually happened: the frame
queued to the pipeline, the document actually inserted into MongoDB, the
rows actually deleted from it.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pipecat.frames.frames import TTSSpeakFrame

from app.models.bot import Bot
from app.models.consent import ConsentRecord
from app.models.conversation import ConversationTurn
from app.pipeline.voice_pipeline import announce_recording_consent
from app.services.retention import purge_expired_transcripts

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeTask:
    """Stands in for PipelineTask — announce_recording_consent's only
    interaction with the real pipeline is queue_frame."""

    def __init__(self):
        self.queued = []

    async def queue_frame(self, frame):
        self.queued.append(frame)


# ---------------------------------------------------------------------------
# The announcement itself
# ---------------------------------------------------------------------------


async def test_the_announcement_is_spoken_when_recording_is_on():
    task = _FakeTask()
    session_id = str(uuid.uuid4())

    await announce_recording_consent(
        task, session_id, "bot-1", "user-1", True, "This call is recorded.",
    )

    assert len(task.queued) == 1
    assert isinstance(task.queued[0], TTSSpeakFrame)
    assert task.queued[0].text == "This call is recorded."


async def test_nothing_is_spoken_when_recording_is_off():
    task = _FakeTask()
    await announce_recording_consent(
        task, str(uuid.uuid4()), "bot-1", "user-1", False, "This call is recorded.",
    )
    assert task.queued == []


async def test_nothing_is_spoken_when_there_is_no_announcement_text():
    """An empty string is a customer's choice not to say anything — must
    not queue a frame that speaks nothing."""
    task = _FakeTask()
    await announce_recording_consent(
        task, str(uuid.uuid4()), "bot-1", "user-1", True, "",
    )
    assert task.queued == []


async def test_a_consent_record_is_written_even_when_recording_is_off():
    """"Log consent as given for each call" — a call where recording was
    OFF and nothing was announced is itself a fact worth an auditable
    record of, not something this only logs on the happy path."""
    task = _FakeTask()
    session_id = str(uuid.uuid4())

    await announce_recording_consent(
        task, session_id, "bot-1", "user-1", False, "This call is recorded.",
    )

    record = await ConsentRecord.find_one(ConsentRecord.session_id == session_id)
    assert record is not None
    assert record.recording_enabled is False
    assert record.announcement_text == "", "logged text for an announcement never spoken"


async def test_a_consent_record_captures_the_exact_wording_spoken():
    """A company's legal team can change the wording over a bot's
    lifetime — what matters for an audit is what was actually SAID on
    this call, not whatever the bot's setting happens to be today."""
    task = _FakeTask()
    session_id = str(uuid.uuid4())

    await announce_recording_consent(
        task, session_id, "bot-1", "user-1", True, "Custom legal wording here.",
    )

    record = await ConsentRecord.find_one(ConsentRecord.session_id == session_id)
    assert record.announcement_text == "Custom legal wording here."
    assert record.recording_enabled is True


async def test_a_logging_failure_never_stops_the_call_greeting(monkeypatch):
    """This runs in the same handler that queues the bot's very first
    words. A database hiccup writing the audit record must not be able to
    leave the bot silent."""
    import app.pipeline.voice_pipeline as vp

    async def _boom(*a, **k):
        raise ConnectionError("mongo is down")

    monkeypatch.setattr(vp.ConsentRecord, "insert", _boom)
    task = _FakeTask()

    await announce_recording_consent(  # must not raise
        task, str(uuid.uuid4()), "bot-1", "user-1", True, "recorded",
    )

    assert len(task.queued) == 1, "the spoken announcement was lost along with the failed log write"


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


async def _make_bot(retention_days: int) -> Bot:
    bot = Bot(
        user_id=str(uuid.uuid4()), name=f"retention-test-{uuid.uuid4().hex[:8]}",
        recording_retention_days=retention_days,
    )
    await bot.insert()
    return bot


async def _make_turn(bot_id: str, age_days: int) -> ConversationTurn:
    turn = ConversationTurn(
        session_id=str(uuid.uuid4()), bot_id=bot_id, bot_name="test",
        user_transcript="hello", assistant_reply="hi",
    )
    await turn.insert()
    # created_at has a default_factory, so it has to be set explicitly
    # after insert to simulate an old row — Motor writes exactly the
    # datetime given here, which is what the query below has to catch.
    turn.created_at = datetime.now(UTC) - timedelta(days=age_days)
    await turn.save()
    return turn


async def test_a_transcript_older_than_retention_is_deleted():
    bot = await _make_bot(retention_days=30)
    old_turn = await _make_turn(str(bot.id), age_days=45)

    deleted = await purge_expired_transcripts()

    assert deleted.get(str(bot.id)) == 1
    assert await ConversationTurn.get(old_turn.id) is None


async def test_a_transcript_within_retention_is_left_alone():
    bot = await _make_bot(retention_days=30)
    recent_turn = await _make_turn(str(bot.id), age_days=5)

    deleted = await purge_expired_transcripts()

    assert deleted.get(str(bot.id), 0) == 0
    assert await ConversationTurn.get(recent_turn.id) is not None


async def test_zero_retention_days_means_keep_forever():
    """The platform default. A bot that never set a number must not have
    its data silently destroyed — that would be its own kind of incident."""
    bot = await _make_bot(retention_days=0)
    old_turn = await _make_turn(str(bot.id), age_days=3650)  # 10 years old

    deleted = await purge_expired_transcripts()

    assert str(bot.id) not in deleted, "a bot with no retention configured was purged anyway"
    assert await ConversationTurn.get(old_turn.id) is not None


async def test_one_bots_retention_never_touches_another_bots_transcripts():
    short_bot = await _make_bot(retention_days=1)
    long_bot = await _make_bot(retention_days=365)
    short_old = await _make_turn(str(short_bot.id), age_days=10)
    long_recent = await _make_turn(str(long_bot.id), age_days=10)

    await purge_expired_transcripts()

    assert await ConversationTurn.get(short_old.id) is None
    assert await ConversationTurn.get(long_recent.id) is not None, (
        "one bot's short retention deleted another bot's transcript"
    )


async def test_consent_records_are_never_touched_by_transcript_retention():
    """The audit trail outlives the recording it is proof of — deleting
    both together would defeat the entire reason ConsentRecord exists."""
    bot = await _make_bot(retention_days=1)
    session_id = str(uuid.uuid4())
    record = ConsentRecord(
        session_id=session_id, bot_id=str(bot.id), user_id=str(bot.user_id),
        recording_enabled=True, announcement_text="recorded",
    )
    await record.insert()
    record.recorded_at = datetime.now(UTC) - timedelta(days=3650)
    await record.save()
    old_turn = await _make_turn(str(bot.id), age_days=10)

    await purge_expired_transcripts()

    assert await ConversationTurn.get(old_turn.id) is None, "the transcript should have been purged"
    assert await ConsentRecord.get(record.id) is not None, (
        "the consent log was deleted along with the transcript it was proof for"
    )


async def test_a_bad_bot_does_not_stop_others_from_being_purged(monkeypatch):
    """One bot's unexpected failure (a corrupt record, a transient driver
    error) must not stop every OTHER bot's retention from being honoured —
    the same reasoning TranscriptRecorder itself uses for save failures."""
    good_bot = await _make_bot(retention_days=1)
    good_old_turn = await _make_turn(str(good_bot.id), age_days=10)

    real_find = ConversationTurn.find
    call_count = {"n": 0}

    def _flaky_find(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ConnectionError("simulated driver failure for the first bot")
        return real_find(*args, **kwargs)

    # A second bot processed BEFORE the good one, to prove the loop
    # continues past a failure rather than stopping at the first one.
    failing_bot = await _make_bot(retention_days=1)
    monkeypatch.setattr(ConversationTurn, "find", _flaky_find)
    try:
        deleted = await purge_expired_transcripts()
    finally:
        monkeypatch.setattr(ConversationTurn, "find", real_find)

    assert str(good_bot.id) in deleted or str(failing_bot.id) in deleted, (
        "a failure on one bot stopped the pass from reaching any other bot"
    )
    # Whichever one wasn't the simulated failure actually got purged.
    await purge_expired_transcripts()  # second real pass, both bots healthy now
    assert await ConversationTurn.get(good_old_turn.id) is None
