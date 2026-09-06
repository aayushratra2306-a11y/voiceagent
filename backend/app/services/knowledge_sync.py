"""Task 7.5 — the actual re-syncing: fetch a source, diff it against what
is already stored, and only touch what changed.

The manual's own tip is the design constraint here, not a nice-to-have:
"only re-index what actually changed. Re-embedding an entire large
document set on every sync is slow and expensive, and it will become your
biggest recurring cost surprisingly quickly." Every item a source returns
is content-hashed, and that hash is compared against what was stored last
time — an unchanged page costs one hash comparison, not a re-embed.

Three outcomes per item the source returns, decided by comparing this
sync's fetch against the last one:

  - new external_id            -> chunk, embed, create a Document
  - known external_id, hash differs -> delete the old vectors, re-embed,
                                        update the SAME Document (never a
                                        second row for the same page)
  - known external_id, hash same    -> nothing. No Pinecone call at all.

A fourth outcome handles the other direction: an external_id that WAS
synced before but is no longer in the source's current results — a page
deleted from Notion, a file removed from the Drive folder, a page taken
off a website — has its Document and vectors removed, the same as if a
customer had deleted it by hand.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

from loguru import logger

from app.core.crypto import decrypt_secret
from app.models.document import Document
from app.models.knowledge_source import KnowledgeSource
from app.services.knowledge_sources import FetchedItem
from app.services.rag import chunk_text, delete_document_vectors, upsert_document


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


async def _fetch_items(source: KnowledgeSource) -> list[FetchedItem]:
    """Dispatches by kind. Adding a new source kind later means one more
    branch here, not a change to the diffing logic below it."""
    if source.kind == "website":
        from app.services.knowledge_sources.website import (
            DEFAULT_MAX_DEPTH,
            DEFAULT_MAX_PAGES,
            crawl_website,
        )

        start_url = source.config.get("start_url", "")
        if not start_url:
            logger.warning(f"[SYNC] website source {source.id} has no start_url configured")
            return []
        return await crawl_website(
            start_url,
            max_pages=source.config.get("max_pages", DEFAULT_MAX_PAGES),
            max_depth=source.config.get("max_depth", DEFAULT_MAX_DEPTH),
        )

    if source.kind == "notion":
        from app.services.knowledge_sources.notion import fetch_notion_pages

        token = decrypt_secret(source.credential_encrypted)
        if not token:
            logger.warning(f"[SYNC] notion source {source.id} has no usable credential")
            return []
        return await fetch_notion_pages(token, source.config)

    if source.kind == "google_drive":
        from app.services.knowledge_sources.google_drive import fetch_drive_files

        key = decrypt_secret(source.credential_encrypted)
        if not key:
            logger.warning(f"[SYNC] google_drive source {source.id} has no usable credential")
            return []
        return await fetch_drive_files(key, source.config)

    logger.warning(f"[SYNC] unknown knowledge source kind: {source.kind!r}")
    return []


async def sync_source(source: KnowledgeSource) -> dict[str, int]:
    """Runs one source's sync end to end and updates its own status fields.

    Never raises: a broken source (an expired Notion token, a website that
    is briefly down) must report ITS OWN error and leave the record of
    what to do about it in `last_sync_error` — not take down the loop that
    is also responsible for every OTHER customer's source.
    """
    stats = {"items_seen": 0, "items_changed": 0, "items_removed": 0}
    try:
        items = await _fetch_items(source)
        stats["items_seen"] = len(items)

        existing_docs = await Document.find(
            Document.source_id == str(source.id)
        ).to_list()
        existing_by_external_id = {d.external_id: d for d in existing_docs}
        seen_external_ids: set[str] = set()

        for item in items:
            seen_external_ids.add(item.external_id)
            new_hash = _hash_text(item.text)
            existing = existing_by_external_id.get(item.external_id)

            if existing is not None and existing.content_hash == new_hash:
                continue  # unchanged — the whole point of hashing at all

            stats["items_changed"] += 1
            chunks = chunk_text([(1, item.text)])

            if existing is not None:
                # Changed: the old vectors are for text that no longer
                # exists under this id, so they are removed before the new
                # ones are written — an unremoved stale vector would let a
                # RAG lookup surface text that was already replaced. Kept
                # under the SAME Document id throughout, so this is one
                # embed call, not two.
                await delete_document_vectors(source.bot_id, str(existing.id), existing.chunk_count)
                await upsert_document(source.bot_id, str(existing.id), chunks)
                existing.filename = item.title
                existing.chunk_count = len(chunks)
                existing.source_url = item.url
                existing.content_hash = new_hash
                await existing.save()
            else:
                # New: the Document is created FIRST so there is a real id
                # to key the vectors by from the start — embedding under a
                # throwaway id and then redoing it under the real one would
                # cost the embedding API call twice for no reason.
                doc = Document(
                    bot_id=source.bot_id,
                    user_id=source.user_id,
                    filename=item.title,
                    chunk_count=len(chunks),
                    source_kind=source.kind,
                    source_id=str(source.id),
                    external_id=item.external_id,
                    source_url=item.url,
                    content_hash=new_hash,
                )
                await doc.insert()
                await upsert_document(source.bot_id, str(doc.id), chunks)

        # The other direction: anything that was synced before but is not
        # in this fetch's results any more no longer exists at the source.
        removed_external_ids = set(existing_by_external_id) - seen_external_ids
        for external_id in removed_external_ids:
            doc = existing_by_external_id[external_id]
            await delete_document_vectors(source.bot_id, str(doc.id), doc.chunk_count)
            await doc.delete()
            stats["items_removed"] += 1

        source.last_synced_at = datetime.now(UTC)
        source.last_sync_status = "ok"
        source.last_sync_error = ""
        source.last_sync_stats = stats
        await source.save()
        logger.info(f"[SYNC] {source.kind} source {source.id}: {stats}")
        return stats

    except Exception as e:
        logger.warning(f"[SYNC] source {source.id} ({source.kind}) failed: {type(e).__name__}: {e}")
        source.last_synced_at = datetime.now(UTC)
        source.last_sync_status = "error"
        source.last_sync_error = f"{type(e).__name__}: {e}"[:500]
        source.last_sync_stats = stats
        try:
            await source.save()
        except Exception as save_error:
            logger.warning(f"[SYNC] could not record failure for source {source.id}: {save_error}")
        return stats


async def sync_due_sources() -> None:
    """One pass: every enabled source whose own interval has elapsed since
    its last sync gets re-synced. A source with no last_synced_at (never
    run) is always due."""
    sources = await KnowledgeSource.find(KnowledgeSource.enabled == True).to_list()  # noqa: E712
    # Naive, deliberately: PyMongo returns datetimes read back from a
    # document as naive (UTC, but with no tzinfo attached) — confirmed
    # directly, and the same reason task 3.8's own outbox comparison once
    # raised "can't subtract offset-naive and offset-aware datetimes" the
    # first time this exact comparison was tried. `last_synced_at` was
    # WRITTEN as datetime.now(UTC) in sync_source() below, but by the time
    # it comes back from a real find(), it is naive — so `now` has to be
    # naive too for the subtraction to mean anything.
    now = datetime.now(UTC).replace(tzinfo=None)

    for source in sources:
        if source.last_synced_at is not None:
            elapsed_minutes = (now - source.last_synced_at).total_seconds() / 60
            if elapsed_minutes < source.sync_interval_minutes:
                continue
        await sync_source(source)


async def knowledge_sync_loop(interval_seconds: int = 300) -> None:
    """Background loop (started from main.py's lifespan). Checks every 5
    minutes for sources whose OWN interval has elapsed — the per-source
    schedule is what actually controls how often a given source is
    re-fetched; this tick rate just bounds how quickly a due source is
    noticed.
    """
    while True:
        try:
            await sync_due_sources()
        except Exception as e:
            logger.error(f"[SYNC] sync pass failed: {e}")
        await asyncio.sleep(interval_seconds)
