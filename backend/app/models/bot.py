from beanie import Document
from pydantic import Field

from app.core.redaction import ALL_KINDS as _DEFAULT_REDACTION_KINDS


class Bot(Document):
    user_id: str
    name: str
    system_prompt: str = "You are a helpful voice assistant."
    voice_id: str = "a0e99841-438c-4a64-b679-ae501e7d6091"  # Cartesia default voice
    llm_model: str = "gpt-4o-mini"
    language: str = "en"

    # Task 6.2 — which categories of sensitive data get masked out of this
    # bot's transcripts before they are ever written to the database. See
    # app/core/redaction.py for the categories and why each rule exists.
    #
    # Defaults to everything: an operator who has not thought about this
    # gets the safe answer (no card numbers stored) rather than silent
    # plaintext storage until someone opts in. Narrowing it — a pizza shop
    # turning off address redaction it will never need — is an informed
    # choice a customer makes, not a default anyone should have to know to
    # ask for.
    redact_transcripts: list[str] = Field(
        default_factory=lambda: sorted(_DEFAULT_REDACTION_KINDS)
    )

    # --- task 3.5, the booking template's configuration -------------------
    # These are what makes booking a form rather than a project: a new
    # customer sets their zone and their hours, and the template's behaviour
    # — availability, clash handling, cancelling, rescheduling — is already
    # written.
    #
    # `timezone` is an IANA name, not an offset. An offset silently breaks
    # twice a year wherever daylight saving applies; the zone name is what
    # carries the rule. Everything is stored in UTC regardless (see
    # models/appointment.py) — this is the zone the caller is spoken to in.
    #
    # Defaulting to Asia/Kolkata rather than UTC because that is where this
    # deployment's callers are, and a wrong-but-plausible local time is a
    # far more visible mistake than a wrong default nobody notices.
    timezone: str = "Asia/Kolkata"
    booking_open: str = "09:00"   # local wall clock, 24-hour
    booking_close: str = "18:00"  # exclusive: the last slot STARTS before this
    slot_minutes: int = 30

    class Settings:
        name = "bots"
