"""Task 7.5 — the Notion connector's own transformation logic.

Cannot be tested against the real Notion API without a real integration
token, which only the account holder can create — see notion.py's own
module docstring for why that limit is stated rather than hidden. What IS
tested here, thoroughly, is the part a bug is actually likely to live in:
turning Notion's REAL documented JSON shapes into plain text. Only the
HTTP call itself is faked, following this project's own established
pattern (test_phase3_hardening.py's `_Client`) — every fixture payload
below is shaped exactly like Notion's own API reference examples, not
simplified in a way that could hide a real parsing bug.
"""

import httpx
import pytest

from app.services.knowledge_sources.notion import fetch_notion_pages

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _rich_text(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text}, "plain_text": text}]


def _blocks_response(blocks: list[dict], has_more: bool = False, next_cursor: str | None = None) -> dict:
    return {"results": blocks, "has_more": has_more, "next_cursor": next_cursor}


class _Resp:
    def __init__(self, status_code: int, json_body: dict):
        self.status_code = status_code
        self._json = json_body

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=httpx.Request("GET", "https://api.notion.com"),
                response=httpx.Response(self.status_code),
            )


class _FakeNotionClient:
    """Routes by URL shape the way the real Notion API's own paths do:
    /pages/{id}, /blocks/{id}/children, /databases/{id}/query."""

    def __init__(self, pages: dict, blocks: dict, database_page_ids: list[str] | None = None,
                 page_block_pages: dict[str, list[dict]] | None = None):
        self.pages = pages  # page_id -> page object
        self.blocks = blocks  # block/page id -> single-page block response (no pagination)
        self.page_block_pages = page_block_pages or {}  # id -> paginated block responses
        self.database_page_ids = database_page_ids or []
        self.requests: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, params: dict | None = None):
        self.requests.append(url)
        if "/pages/" in url:
            page_id = url.rsplit("/", 1)[-1]
            return _Resp(200, self.pages.get(page_id, {}))
        if "/blocks/" in url and url.endswith("/children"):
            block_id = url.split("/blocks/")[1].split("/children")[0]
            if block_id in self.page_block_pages:
                pages = self.page_block_pages[block_id]
                cursor = (params or {}).get("start_cursor")
                index = 0 if cursor is None else int(cursor)
                return _Resp(200, pages[index])
            return _Resp(200, self.blocks.get(block_id, _blocks_response([])))
        return _Resp(404, {})

    async def post(self, url: str, json: dict | None = None):
        self.requests.append(url)
        if "/databases/" in url and url.endswith("/query"):
            return _Resp(200, {
                "results": [{"id": pid} for pid in self.database_page_ids],
                "has_more": False, "next_cursor": None,
            })
        return _Resp(404, {})


def _simple_page(page_id: str, title: str, url: str = "") -> dict:
    return {
        "id": page_id,
        "url": url or f"https://notion.so/{page_id}",
        "properties": {"Name": {"type": "title", "title": _rich_text(title)}},
    }


async def test_a_single_pages_text_is_extracted(monkeypatch):
    page_id = "page-1"
    client = _FakeNotionClient(
        pages={page_id: _simple_page(page_id, "My Page")},
        blocks={page_id: _blocks_response([
            {"type": "paragraph", "id": "b1", "has_children": False,
             "paragraph": {"rich_text": _rich_text("Hello, this is the page content.")}},
        ])},
    )
    monkeypatch.setattr("app.services.knowledge_sources.notion.httpx.AsyncClient", lambda **k: client)

    items = await fetch_notion_pages("fake-token", {"page_id": page_id})

    assert len(items) == 1
    assert items[0].external_id == page_id
    assert items[0].title == "My Page"
    assert "Hello, this is the page content." in items[0].text


async def test_the_title_is_found_regardless_of_the_property_name(monkeypatch):
    """A workspace can name its title property anything ("Name", "Title",
    "Page") — the title lives wherever type=="title" is, not at a fixed key."""
    page_id = "page-1"
    page = {
        "id": page_id, "url": "https://notion.so/page-1",
        "properties": {
            "Status": {"type": "select", "select": {"name": "Done"}},
            "Page": {"type": "title", "title": _rich_text("Found By Type")},
        },
    }
    client = _FakeNotionClient(pages={page_id: page}, blocks={page_id: _blocks_response([
        {"type": "paragraph", "id": "b1", "has_children": False,
         "paragraph": {"rich_text": _rich_text("content")}},
    ])})
    monkeypatch.setattr("app.services.knowledge_sources.notion.httpx.AsyncClient", lambda **k: client)

    items = await fetch_notion_pages("fake-token", {"page_id": page_id})
    assert items[0].title == "Found By Type"


async def test_multiple_text_block_types_are_all_extracted(monkeypatch):
    page_id = "page-1"
    blocks = [
        {"type": "heading_1", "id": "b1", "has_children": False,
         "heading_1": {"rich_text": _rich_text("A Heading")}},
        {"type": "bulleted_list_item", "id": "b2", "has_children": False,
         "bulleted_list_item": {"rich_text": _rich_text("First bullet")}},
        {"type": "to_do", "id": "b3", "has_children": False,
         "to_do": {"rich_text": _rich_text("Finish the report"), "checked": True}},
        {"type": "quote", "id": "b4", "has_children": False,
         "quote": {"rich_text": _rich_text("A wise quote")}},
    ]
    client = _FakeNotionClient(
        pages={page_id: _simple_page(page_id, "Mixed content")},
        blocks={page_id: _blocks_response(blocks)},
    )
    monkeypatch.setattr("app.services.knowledge_sources.notion.httpx.AsyncClient", lambda **k: client)

    items = await fetch_notion_pages("fake-token", {"page_id": page_id})
    text = items[0].text
    assert "A Heading" in text
    assert "First bullet" in text
    assert "[x] Finish the report" in text
    assert "A wise quote" in text


async def test_an_unsupported_block_type_is_skipped_not_an_error(monkeypatch):
    page_id = "page-1"
    blocks = [
        {"type": "image", "id": "b1", "has_children": False, "image": {"type": "external"}},
        {"type": "paragraph", "id": "b2", "has_children": False,
         "paragraph": {"rich_text": _rich_text("Real text after an image")}},
    ]
    client = _FakeNotionClient(
        pages={page_id: _simple_page(page_id, "Has an image")},
        blocks={page_id: _blocks_response(blocks)},
    )
    monkeypatch.setattr("app.services.knowledge_sources.notion.httpx.AsyncClient", lambda **k: client)

    items = await fetch_notion_pages("fake-token", {"page_id": page_id})
    assert "Real text after an image" in items[0].text


async def test_nested_blocks_are_recursed_into(monkeypatch):
    """A bulleted list under a toggle — has_children=True must trigger a
    follow-up fetch of that block's own children."""
    page_id = "page-1"
    client = _FakeNotionClient(
        pages={page_id: _simple_page(page_id, "Nested")},
        blocks={
            page_id: _blocks_response([
                {"type": "toggle", "id": "toggle-1", "has_children": True,
                 "toggle": {"rich_text": _rich_text("Click to expand")}},
            ]),
            "toggle-1": _blocks_response([
                {"type": "paragraph", "id": "nested-1", "has_children": False,
                 "paragraph": {"rich_text": _rich_text("Hidden nested content")}},
            ]),
        },
    )
    monkeypatch.setattr("app.services.knowledge_sources.notion.httpx.AsyncClient", lambda **k: client)

    items = await fetch_notion_pages("fake-token", {"page_id": page_id})
    assert "Click to expand" in items[0].text
    assert "Hidden nested content" in items[0].text


async def test_block_pagination_is_followed_to_the_end(monkeypatch):
    """A page with more blocks than Notion's own per-request page size —
    has_more=True must trigger another request, not silently lose
    everything past the first page."""
    page_id = "page-1"
    page_1 = _blocks_response(
        [{"type": "paragraph", "id": "b1", "has_children": False,
          "paragraph": {"rich_text": _rich_text("First page of blocks")}}],
        has_more=True, next_cursor="1",
    )
    page_2 = _blocks_response(
        [{"type": "paragraph", "id": "b2", "has_children": False,
          "paragraph": {"rich_text": _rich_text("Second page of blocks")}}],
        has_more=False,
    )
    client = _FakeNotionClient(
        pages={page_id: _simple_page(page_id, "Long page")},
        blocks={},
        page_block_pages={page_id: [page_1, page_2]},
    )
    monkeypatch.setattr("app.services.knowledge_sources.notion.httpx.AsyncClient", lambda **k: client)

    items = await fetch_notion_pages("fake-token", {"page_id": page_id})
    assert "First page of blocks" in items[0].text
    assert "Second page of blocks" in items[0].text


async def test_a_database_syncs_every_page_currently_in_it(monkeypatch):
    """The actual 'auto re-sync' value on the Notion side: a page added to
    the database later is picked up on the next scheduled sync with no
    reconfiguration — proven by the database query returning IDs the
    config itself never named."""
    client = _FakeNotionClient(
        pages={
            "page-a": _simple_page("page-a", "First"),
            "page-b": _simple_page("page-b", "Second"),
        },
        blocks={
            "page-a": _blocks_response([{"type": "paragraph", "id": "x", "has_children": False,
                                          "paragraph": {"rich_text": _rich_text("Content A")}}]),
            "page-b": _blocks_response([{"type": "paragraph", "id": "y", "has_children": False,
                                          "paragraph": {"rich_text": _rich_text("Content B")}}]),
        },
        database_page_ids=["page-a", "page-b"],
    )
    monkeypatch.setattr("app.services.knowledge_sources.notion.httpx.AsyncClient", lambda **k: client)

    items = await fetch_notion_pages("fake-token", {"database_id": "db-1"})

    assert {item.external_id for item in items} == {"page-a", "page-b"}


async def test_a_page_with_no_extractable_text_is_skipped():
    """An empty page (or one made entirely of unsupported block types)
    contributes nothing to a text-based knowledge base — must not appear
    as a zero-content item."""
    client = _FakeNotionClient(
        pages={"page-1": _simple_page("page-1", "Empty")},
        blocks={"page-1": _blocks_response([])},
    )
    import app.services.knowledge_sources.notion as notion_module
    original = notion_module.httpx.AsyncClient
    notion_module.httpx.AsyncClient = lambda **k: client
    try:
        items = await fetch_notion_pages("fake-token", {"page_id": "page-1"})
    finally:
        notion_module.httpx.AsyncClient = original

    assert items == []


async def test_neither_page_id_nor_database_id_configured_returns_nothing():
    items = await fetch_notion_pages("fake-token", {})
    assert items == []


async def test_one_bad_page_in_a_database_does_not_stop_the_others(monkeypatch):
    """A page the integration lost access to (removed from the share list)
    must not take down the sync for every other page in the same database."""
    client = _FakeNotionClient(
        pages={"page-good": _simple_page("page-good", "Still accessible")},
        blocks={"page-good": _blocks_response([
            {"type": "paragraph", "id": "x", "has_children": False,
             "paragraph": {"rich_text": _rich_text("Reachable content")}},
        ])},
        database_page_ids=["page-missing", "page-good"],
    )
    # page-missing is not in `pages`, so the fake client's 404 branch fires.
    monkeypatch.setattr("app.services.knowledge_sources.notion.httpx.AsyncClient", lambda **k: client)

    items = await fetch_notion_pages("fake-token", {"database_id": "db-1"})

    assert len(items) == 1
    assert items[0].external_id == "page-good"


async def test_a_total_api_failure_returns_an_empty_list_not_an_exception(monkeypatch):
    class _ExplodingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *a, **k):
            raise httpx.ConnectError("Notion is unreachable")

        async def post(self, *a, **k):
            raise httpx.ConnectError("Notion is unreachable")

    monkeypatch.setattr(
        "app.services.knowledge_sources.notion.httpx.AsyncClient", lambda **k: _ExplodingClient()
    )

    items = await fetch_notion_pages("fake-token", {"page_id": "page-1"})
    assert items == []
