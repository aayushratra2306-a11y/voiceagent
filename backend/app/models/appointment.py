from datetime import UTC, datetime

from beanie import Document
from pydantic import Field


class Appointment(Document):
    """Task 1.4's booking tool demo — records what the caller booked, so the
    tool can also detect and refuse a double-booked slot."""

    date: str  # e.g. "2026-09-03" — the booking tool normalizes to this before saving
    time: str  # e.g. "15:00" — 24-hour, normalized before saving
    purpose: str
    booked_by: str = "voice caller"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "appointments"
