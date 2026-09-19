from datetime import UTC, datetime
from typing import Any

from beanie import Document, Indexed
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel


class ConversationTurn(Document):
    """Task 1.5 — one document per completed turn (not one growing array per
    session): scales far better once this collection has real volume, and
    keeps every turn independently indexable and queryable.

    Timing fields exist specifically so a future "it feels slow" complaint
    can be answered by a query instead of a manual log-timestamp hunt — which
    is exactly how today's session diagnosed the ~1.5s Groq/RAG latency by
    hand, repeatedly, from raw log lines.
    """

    session_id: str  # groups every turn from one voice session together
    bot_id: str | None = None
    # Task 5.1 — the owning organisation. user_id stays as "who created it"
    # and is no longer used for access. Blank = not yet migrated = invisible.
    org_id: str = ""
    bot_name: str = ""

    user_transcript: str = ""
    assistant_reply: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)

    user_stopped_speaking_at: datetime | None = None
    llm_first_response_at: datetime | None = None
    bot_started_speaking_at: datetime | None = None
    bot_stopped_speaking_at: datetime | None = None

    # Computed once the turn completes — see TranscriptRecorder._finalize_turn.
    # time_to_first_token_ms isolates the LLM/RAG cost specifically;
    # time_to_speech_ms is the full "how long did the caller actually wait"
    # number — the two together tell you whether slowness is in generation
    # or in TTS, without re-deriving it from logs.
    time_to_first_token_ms: int | None = None
    time_to_speech_ms: int | None = None

    created_at: Indexed(datetime) = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "conversation_turns"
        indexes = [
            "bot_id",
            "session_id",
            IndexModel(
                [("org_id", ASCENDING), ("bot_id", ASCENDING), ("created_at", DESCENDING)],
                name="turns_by_org_bot_time",
            ),
        ]
