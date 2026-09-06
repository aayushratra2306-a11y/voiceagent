from datetime import UTC, datetime

from beanie import Document, Indexed
from pydantic import Field


class ConsentRecord(Document):
    """Task 6.3 — proof that a caller was told they were being recorded.

    A separate collection from ConversationTurn on purpose, not a field on
    the first turn: consent has to be provable even for a call that has NO
    turns at all — the caller hears the announcement and hangs up
    immediately, before saying anything a transcript would capture. If
    consent lived only on turn one, that call would look, to an auditor, as
    though nobody was ever told.

    Written once, at call start, from run_voice_pipeline — see
    `announce_recording_consent`. Never deleted by the retention job
    (app/services/retention.py) that expires transcripts: deleting the
    proof of consent along with the recording it was proof for defeats the
    reason it exists. This collection is the evidence that survives even
    after the conversation it describes does not.
    """

    session_id: str
    bot_id: str | None = None
    # The bot's OWNER — task 6.3's "per-customer" scoping — kept alongside
    # bot_id the same way webhooks.py keeps a bot's owner on webhook events
    # (task 3.8), so a query for "every consent I collected" needs no join
    # back through the bot to work.
    user_id: str | None = None

    # Whether recording was actually on for this call, and the exact words
    # spoken — a company's legal team specifies precise wording (the
    # manual's own tip), and that wording can change over the life of a
    # bot, so the announcement actually given is worth keeping verbatim
    # rather than assuming it matches whatever the bot's CURRENT setting is.
    recording_enabled: bool
    announcement_text: str = ""

    recorded_at: Indexed(datetime) = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "consent_records"
        indexes = ["session_id", "bot_id", "user_id"]
