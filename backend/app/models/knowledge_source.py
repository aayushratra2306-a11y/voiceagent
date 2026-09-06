"""Task 7.5 — a knowledge source a bot's documents are kept in sync with,
as opposed to a one-off manual upload (task 2.10).

One record per source a customer has connected — a website root URL, a
Notion page/database, a Google Drive folder — and the sync loop in
app/services/knowledge_sync.py works through these on a schedule,
re-fetching each and updating the bot's Document rows to match.
"""

from datetime import UTC, datetime
from typing import Any

from beanie import Document as BeanieDocument
from pydantic import Field


class KnowledgeSource(BeanieDocument):
    bot_id: str
    user_id: str

    # "website" | "notion" | "google_drive" — see
    # app/services/knowledge_sources/ for the fetcher each one runs through.
    kind: str

    # Kind-specific, plain (non-secret) configuration:
    #   website:      {"start_url": "...", "max_pages": 50, "max_depth": 3}
    #   notion:       {"page_id": "..."} or {"database_id": "..."}
    #   google_drive: {"folder_id": "..."}
    config: dict[str, Any] = Field(default_factory=dict)

    # A Notion integration token or a Google service-account JSON key —
    # encrypted at rest the same way a bot tool's own API key already is
    # (app/core/crypto.py), for the same reason: this is a credential this
    # system must be able to USE later, not merely verify, so it has to be
    # recoverable rather than hashed. Blank for "website", which needs no
    # credential to fetch a public page.
    credential_encrypted: str = ""

    # How often the sync loop should revisit this source. Per-source, not a
    # single global number: a website that rarely changes and a Notion
    # workspace edited daily do not want the same schedule, and the manual's
    # own worry about sync cost (re-embedding being "your biggest recurring
    # cost surprisingly quickly") means a customer should be able to turn
    # this down for a source that does not need to be checked often.
    sync_interval_minutes: int = 60

    enabled: bool = True

    # What happened last time, so a customer can see sync health without
    # reading server logs — the manual's own "show sync status and errors
    # clearly in the interface" step.
    last_synced_at: datetime | None = None
    last_sync_status: str = "never"  # "never" | "ok" | "error"
    last_sync_error: str = ""
    # {"items_seen": N, "items_changed": N, "items_removed": N} — lets an
    # operator tell "nothing changed, as expected" apart from "the source
    # returned nothing, which might mean it's broken" at a glance.
    last_sync_stats: dict[str, int] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "knowledge_sources"
        indexes = ["bot_id"]
