from datetime import UTC, datetime

from beanie import Document
from pydantic import Field


class Appointment(Document):
    """A booking made on a call.

    Task 1.4 created this as a demo with two string fields. Task 3.5 turned
    it into the booking template's record, and the change that matters is
    `starts_at_utc`.

    The manual's warning on that task is that time zones will hurt you, and
    the rule it gives is the one followed here: store UTC, convert only for
    display and speech. `date` and `time` are kept alongside it as the
    caller's LOCAL wall-clock time, which is what gets spoken back — they
    are a rendering of `starts_at_utc` in `timezone`, never the source of
    truth. Rows written before 3.5 have only those two and a null
    `starts_at_utc`; nothing reads the UTC field without checking, so those
    rows stay readable.

    `reference` is what the caller is given out loud and repeats back to
    cancel or move the booking. Deliberately short and unambiguous when
    spoken — see booking.py's alphabet, which has no letters or digits that
    sound alike.
    """

    date: str  # local wall-clock date, e.g. "2026-09-03" — for speaking back
    time: str  # local wall-clock time, 24-hour, e.g. "15:00"
    purpose: str
    booked_by: str = "voice caller"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # --- task 3.5 ---------------------------------------------------------
    # The single source of truth for when this is. Optional only so rows
    # written before 3.5 still load.
    starts_at_utc: datetime | None = None
    # The zone `date`/`time` are expressed in, so a booking can still be
    # spoken correctly if the bot's configured zone changes later.
    timezone: str = "UTC"
    duration_minutes: int = 30
    # Spoken to the caller; used to find the booking again to move or cancel.
    reference: str = ""
    status: str = "booked"  # booked | cancelled
    # Which bot took it. Blank for rows written before 3.5.
    bot_id: str = ""
    caller_name: str = ""
    # The key held in the booking_slots collection while this is live. Kept
    # so cancelling can release exactly the slot this booking took, rather
    # than recomputing it and risking releasing someone else's.
    slot_key: str = ""

    class Settings:
        name = "appointments"
