from datetime import UTC, datetime

from beanie import Document, Indexed
from pydantic import Field


class GuardrailIncident(Document):
    """Task 6.1 — "log every blocked attempt for review," the manual's own
    step 6. One row per detection from either half of app/core/guardrails.py:

      - direction="input"  — the CALLER said something matching a known
        manipulation pattern (app/core/guardrails.check_caller_input).
        Logged for visibility only; the model is already instructed to
        resist these regardless (GUARDRAIL_RULE) — this is what lets an
        operator see how often, and how, real callers actually try.
      - direction="output" — the BOT's own reply leaked a chunk of its
        system prompt, or mentioned a per-bot forbidden topic
        (app/core/guardrails.check_output). Unlike an input incident, an
        output one means the flagged sentence was actually intercepted and
        replaced before being spoken — this row is the record of a
        prevention, not just a suspicion.

    `snippet` is passed through task 6.2's redaction rules before storage
    (see guardrails.log_incident) — a security log is not exempt from the
    same card-number liability a transcript is; an adversarial caller
    reading out a real card number while trying a jailbreak phrase is a
    perfectly plausible way for one to end up here otherwise.
    """

    session_id: str
    bot_id: str | None = None
    user_id: str | None = None  # the bot's OWNER, same scoping as ConsentRecord

    direction: str  # "input" | "output"
    category: str  # e.g. "override_instructions", "prompt_leak", "forbidden_topic:layoffs"
    snippet: str = ""

    detected_at: Indexed(datetime) = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "guardrail_incidents"
        indexes = ["session_id", "bot_id", "user_id", "category"]
