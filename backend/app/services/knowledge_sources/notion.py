"""Task 7.5 — pull page content out of Notion via its own REST API.

Uses a Notion **internal integration token**, not an OAuth consent flow.
Notion's own developer docs (notion.so/my-integrations) name this as the
supported path for exactly this shape of use — a customer's own backend
reading their own workspace — and it needs no registered OAuth app, no
redirect URI hosted on this project's domain, and no consent screen. A
customer creates the integration in their own Notion workspace, shares the
specific page or database with it, and pastes the token into this bot's
knowledge source config. That is the credential `KnowledgeSource.
credential_encrypted` stores (encrypted with the same Fernet scheme as
every other stored credential in this project — app/core/crypto.py).

**Honest limit, stated rather than left to be discovered**: nothing in
this module can be exercised against the real Notion API without a real
integration token, which only the account holder can create. What IS
tested (tests/test_notion_source.py) is the transformation logic — turning
Notion's actual documented JSON shapes into plain text — against realistic
fixture payloads, with only the HTTP call itself mocked. That is the part
a bug is actually likely to live in (a block type this code does not
handle, pagination not followed to the end), and it is fully verifiable
without a live token. Whether authentication itself succeeds against a
real workspace is the one thing only the account holder's own token can
prove — the same category of limit as Razorpay TEST MODE or the Sentry
DSN elsewhere in this project.
"""

from __future__ import annotations

import httpx
from loguru import logger

from app.services.knowledge_sources import FetchedItem

API_BASE = "https://api.notion.com/v1"
# Pinned to a specific, dated version rather than "latest" — Notion's own
# docs are explicit that the API can change between dated versions, and an
# unpinned integration silently breaking on Notion's own schedule is a much
# worse failure mode than needing to bump this deliberately later.
NOTION_VERSION = "2022-06-28"
REQUEST_TIMEOUT_SECONDS = 15.0

# Rich text belonging to any of these block types is extracted as plain
# text. Anything else (images, embeds, dividers, unsupported block types
# Notion adds later) is skipped rather than guessed at — a block with no
# text content contributes nothing to a knowledge base built from text
# anyway.
_TEXT_BLOCK_TYPES = frozenset({
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "to_do", "toggle",
    "quote", "callout", "code",
})


def _plain_text_from_rich_text(rich_text: list[dict]) -> str:
    return "".join(fragment.get("plain_text", "") for fragment in rich_text)


def _block_to_text(block: dict) -> str:
    block_type = block.get("type")
    if block_type not in _TEXT_BLOCK_TYPES:
        return ""
    payload = block.get(block_type, {})
    text = _plain_text_from_rich_text(payload.get("rich_text", []))
    if block_type == "to_do" and payload.get("checked"):
        return f"[x] {text}"
    if block_type == "to_do":
        return f"[ ] {text}"
    return text


async def _fetch_block_children(client: httpx.AsyncClient, block_id: str) -> list[dict]:
    """Every child block of a page or a nested block, following
    pagination to the end — a page with more than 100 blocks (Notion's
    per-request page size) would otherwise silently lose everything past
    the first page."""
    blocks: list[dict] = []
    cursor: str | None = None
    while True:
        params = {"start_cursor": cursor} if cursor else {}
        response = await client.get(f"{API_BASE}/blocks/{block_id}/children", params=params)
        response.raise_for_status()
        body = response.json()
        blocks.extend(body.get("results", []))
        if not body.get("has_more"):
            return blocks
        cursor = body.get("next_cursor")
        if not cursor:  # defensive: has_more=True with no cursor would loop forever
            return blocks


async def _page_text(client: httpx.AsyncClient, page_id: str, _depth: int = 0) -> str:
    """Flattens a page's blocks into plain text, recursing into nested
    blocks (a bulleted list under a toggle, for instance).

    _depth caps recursion at a sane bound — Notion pages can nest blocks
    arbitrarily, and a page that happens to be extremely deeply nested
    should degrade to "the deepest levels are omitted," not stack-overflow
    a sync that was supposed to be a background job.
    """
    if _depth > 20:
        return ""

    blocks = await _fetch_block_children(client, page_id)
    lines: list[str] = []
    for block in blocks:
        text = _block_to_text(block)
        if text:
            lines.append(text)
        if block.get("has_children"):
            nested = await _page_text(client, block["id"], _depth + 1)
            if nested:
                lines.append(nested)
    return "\n".join(lines)


def _page_title(page: dict) -> str:
    """A page's title lives in whichever property has type "title" — its
    NAME varies ("Name", "Title", anything a workspace calls it), so this
    has to search by type rather than assume a property name."""
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return _plain_text_from_rich_text(prop.get("title", []))
    return "Untitled"


async def _fetch_database_page_ids(client: httpx.AsyncClient, database_id: str) -> list[str]:
    ids: list[str] = []
    cursor: str | None = None
    while True:
        body = {"start_cursor": cursor} if cursor else {}
        response = await client.post(f"{API_BASE}/databases/{database_id}/query", json=body)
        response.raise_for_status()
        result = response.json()
        ids.extend(page["id"] for page in result.get("results", []))
        if not result.get("has_more"):
            return ids
        cursor = result.get("next_cursor")
        if not cursor:
            return ids


async def fetch_notion_pages(token: str, config: dict) -> list[FetchedItem]:
    """`config` carries exactly one of `page_id` (sync a single page) or
    `database_id` (sync every page currently in that database — new pages
    added to the database are picked up on the next scheduled sync with no
    reconfiguration needed, which is the actual "auto re-syncing" value
    task 7.5 asks for on the Notion side).

    Never raises: an expired token, a page the integration lost access to,
    or a Notion outage should mean this source's sync reports an error and
    every OTHER configured source still runs — the same reasoning
    website.py's own crawl uses for one unreachable page.
    """
    page_ids: list[str]
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
    }

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, headers=headers) as client:
        try:
            if config.get("database_id"):
                page_ids = await _fetch_database_page_ids(client, config["database_id"])
            elif config.get("page_id"):
                page_ids = [config["page_id"]]
            else:
                logger.warning("[NOTION] knowledge source has neither page_id nor database_id configured")
                return []

            items: list[FetchedItem] = []
            for page_id in page_ids:
                try:
                    page_response = await client.get(f"{API_BASE}/pages/{page_id}")
                    page_response.raise_for_status()
                    page = page_response.json()
                    text = await _page_text(client, page_id)
                    if not text.strip():
                        continue
                    items.append(FetchedItem(
                        external_id=page_id,
                        url=page.get("url", ""),
                        title=_page_title(page),
                        text=text,
                    ))
                except httpx.HTTPStatusError as e:
                    logger.warning(f"[NOTION] could not fetch page {page_id}: {e}")
                    continue
            return items
        except httpx.HTTPStatusError as e:
            logger.warning(f"[NOTION] sync failed: {e}")
            return []
        except Exception as e:
            logger.warning(f"[NOTION] unexpected error during sync: {type(e).__name__}: {e}")
            return []
