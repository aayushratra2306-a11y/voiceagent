"""Task 7.5 — configuring a bot's website/Notion/Google Drive knowledge
sources, and the manual's own "show sync status and errors clearly in the
interface" step.

A credential (a Notion integration token, a Drive service-account JSON
key) is write-only through this API — accepted on create, encrypted
immediately (app/core/crypto.py, the same scheme every other stored
credential in this project already uses), and never echoed back. The same
pattern app/api/bot_tools.py already uses for a tool's own auth secret.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.core.auth import get_current_user
from app.core.crypto import encrypt_secret
from app.core.deps import get_owned_bot, get_owned_knowledge_source
from app.models.bot import Bot
from app.models.document import Document
from app.models.knowledge_source import KnowledgeSource
from app.models.user import User
from app.services.knowledge_sync import sync_source
from app.services.rag import delete_document_vectors

router = APIRouter(tags=["knowledge-sources"])

VALID_KINDS = {"website", "notion", "google_drive"}

MAX_WEBSITE_PAGES = 200  # a hard ceiling regardless of what a customer requests
MAX_WEBSITE_DEPTH = 10


class KnowledgeSourceCreate(BaseModel):
    kind: str
    config: dict = Field(default_factory=dict)
    # Plaintext in the request, encrypted before it ever reaches storage;
    # blank for "website", which needs no credential.
    credential: str = ""
    sync_interval_minutes: int = Field(default=60, ge=5, le=10080)  # 5 min .. 1 week

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        if v not in VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(VALID_KINDS)}")
        return v


class KnowledgeSourceUpdate(BaseModel):
    config: dict | None = None
    credential: str | None = None  # None = leave the stored one unchanged
    sync_interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    enabled: bool | None = None


def _validate_config(kind: str, config: dict) -> None:
    """Checked at configuration time so a customer finds out about a typo
    immediately rather than from a sync that silently does nothing — the
    same reasoning bots.py's own field validators already follow for
    every other per-bot setting."""
    if kind == "website":
        start_url = config.get("start_url", "")
        parsed = urlparse(start_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(
                status_code=422,
                detail="config.start_url must be a full http(s) URL, e.g. https://example.com",
            )
        max_pages = config.get("max_pages")
        if max_pages is not None and not (1 <= max_pages <= MAX_WEBSITE_PAGES):
            raise HTTPException(
                status_code=422, detail=f"config.max_pages must be between 1 and {MAX_WEBSITE_PAGES}",
            )
        max_depth = config.get("max_depth")
        if max_depth is not None and not (0 <= max_depth <= MAX_WEBSITE_DEPTH):
            raise HTTPException(
                status_code=422, detail=f"config.max_depth must be between 0 and {MAX_WEBSITE_DEPTH}",
            )
    elif kind == "notion":
        if not config.get("page_id") and not config.get("database_id"):
            raise HTTPException(
                status_code=422, detail="config must set exactly one of page_id or database_id",
            )
        if config.get("page_id") and config.get("database_id"):
            raise HTTPException(
                status_code=422, detail="config must set only ONE of page_id or database_id, not both",
            )
    elif kind == "google_drive":
        if not config.get("folder_id"):
            raise HTTPException(status_code=422, detail="config must set folder_id")


def _requires_credential(kind: str) -> bool:
    return kind in ("notion", "google_drive")


def _out(source: KnowledgeSource) -> dict:
    """Never includes the credential — write-only, per this module's own
    docstring."""
    return {
        "id": str(source.id),
        "bot_id": source.bot_id,
        "kind": source.kind,
        "config": source.config,
        "has_credential": bool(source.credential_encrypted),
        "sync_interval_minutes": source.sync_interval_minutes,
        "enabled": source.enabled,
        "last_synced_at": source.last_synced_at.isoformat() if source.last_synced_at else None,
        "last_sync_status": source.last_sync_status,
        "last_sync_error": source.last_sync_error,
        "last_sync_stats": source.last_sync_stats,
    }


@router.post("/bots/{bot_id}/knowledge-sources", status_code=201)
async def create_knowledge_source(
    body: KnowledgeSourceCreate,
    bot: Bot = Depends(get_owned_bot),
    current_user: User = Depends(get_current_user),
):
    _validate_config(body.kind, body.config)
    if _requires_credential(body.kind) and not body.credential:
        raise HTTPException(
            status_code=422,
            detail=f"{body.kind} requires a credential (see the manual for how to create one)",
        )

    source = KnowledgeSource(
        bot_id=str(bot.id),
        user_id=str(current_user.id),
        kind=body.kind,
        config=body.config,
        credential_encrypted=encrypt_secret(body.credential) if body.credential else "",
        sync_interval_minutes=body.sync_interval_minutes,
    )
    await source.insert()
    return _out(source)


@router.get("/bots/{bot_id}/knowledge-sources")
async def list_knowledge_sources(bot: Bot = Depends(get_owned_bot)):
    sources = await KnowledgeSource.find(KnowledgeSource.bot_id == str(bot.id)).to_list()
    return [_out(s) for s in sources]


@router.patch("/knowledge-sources/{source_id}")
async def update_knowledge_source(
    body: KnowledgeSourceUpdate, source: KnowledgeSource = Depends(get_owned_knowledge_source),
):
    if body.config is not None:
        _validate_config(source.kind, body.config)
        source.config = body.config
    if body.credential is not None:
        source.credential_encrypted = encrypt_secret(body.credential) if body.credential else ""
    if body.sync_interval_minutes is not None:
        source.sync_interval_minutes = body.sync_interval_minutes
    if body.enabled is not None:
        source.enabled = body.enabled
    await source.save()
    return _out(source)


@router.post("/knowledge-sources/{source_id}/sync")
async def trigger_sync(source: KnowledgeSource = Depends(get_owned_knowledge_source)):
    """Manual "sync now" — the manual's own emphasis on visible sync status
    is not very useful if a customer who just fixed a broken credential has
    to wait for the next scheduled tick to find out whether it worked."""
    stats = await sync_source(source)
    refreshed = await KnowledgeSource.get(source.id)
    return {**_out(refreshed), "this_sync": stats}


@router.delete("/knowledge-sources/{source_id}", status_code=204)
async def delete_knowledge_source(source: KnowledgeSource = Depends(get_owned_knowledge_source)):
    """Removes the source AND every Document/vector it ever produced — the
    same cleanup documents.py's own delete_document already does for a
    manual upload, so a deleted source does not leave orphaned content a
    bot keeps citing after the customer disconnected it."""
    docs = await Document.find(Document.source_id == str(source.id)).to_list()
    for doc in docs:
        await delete_document_vectors(doc.bot_id, str(doc.id), doc.chunk_count)
        await doc.delete()
    await source.delete()
