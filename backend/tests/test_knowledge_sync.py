"""Task 7.5 — the sync engine's diffing logic: this is the actual value the
manual's tip is about. "Only re-index what actually changed" is not a
performance nicety here, it's the difference between a knowledge source
being affordable to run on a schedule and not.

Pinecone/OpenAI embedding calls (app.services.rag.upsert_document /
delete_document_vectors) are mocked throughout — this file's job is
proving the DECISION of what to re-embed is correct, not exercising the
real embedding pipeline, which has its own tests and its own real
external-service cost. Fetching from an actual source is mocked too
(website.py/notion.py/google_drive.py each have their own dedicated test
files for that half).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.models.document import Document
from app.models.knowledge_source import KnowledgeSource
from app.services.knowledge_sources import FetchedItem
from app.services.knowledge_sync import sync_due_sources, sync_source

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _clean_knowledge_sources():
    """sync_due_sources() scans EVERY enabled KnowledgeSource with no
    filter — correct in production (that is the whole point of a
    scheduler pass), but it means a source left behind by an earlier test
    in this file is picked up by a later one's own sync_due_sources() call,
    which is exactly the kind of cross-test interference that produces a
    failure with no relation to what the failing test actually does."""
    yield
    await KnowledgeSource.find_all().delete()


@pytest.fixture
def upsert_calls(monkeypatch):
    """Replaces the real embedding pipeline with a call-recording fake —
    what matters here is HOW OFTEN and on WHICH doc id it gets called, not
    what it returns."""
    calls = {"upsert": [], "delete": []}

    async def _fake_upsert(bot_id, doc_id, chunks):
        calls["upsert"].append((bot_id, doc_id, len(chunks)))
        return len(chunks)

    async def _fake_delete(bot_id, doc_id, chunk_count):
        calls["delete"].append((bot_id, doc_id, chunk_count))

    monkeypatch.setattr("app.services.knowledge_sync.upsert_document", _fake_upsert)
    monkeypatch.setattr("app.services.knowledge_sync.delete_document_vectors", _fake_delete)
    return calls


async def _make_source(**overrides) -> KnowledgeSource:
    defaults = dict(
        bot_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()), kind="website",
        config={"start_url": "https://example.com"},
    )
    defaults.update(overrides)
    source = KnowledgeSource(**defaults)
    await source.insert()
    return source


def _mock_fetch(monkeypatch, items: list[FetchedItem]):
    async def _fake(_source):
        return items

    monkeypatch.setattr("app.services.knowledge_sync._fetch_items", _fake)


async def test_a_brand_new_item_is_embedded_and_a_document_is_created(monkeypatch, upsert_calls):
    source = await _make_source()
    _mock_fetch(monkeypatch, [FetchedItem("page-1", "https://example.com/1", "Page 1", "Some real content here")])

    stats = await sync_source(source)

    assert stats["items_seen"] == 1
    assert stats["items_changed"] == 1
    assert len(upsert_calls["upsert"]) == 1

    doc = await Document.find_one(Document.source_id == str(source.id))
    assert doc is not None
    assert doc.external_id == "page-1"
    assert doc.source_kind == "website"
    assert doc.filename == "Page 1"


async def test_resyncing_unchanged_content_never_calls_the_embedder_again(monkeypatch, upsert_calls):
    """The manual's own point, checked directly: nothing costs an
    embedding call on the second sync if nothing changed."""
    source = await _make_source()
    item = FetchedItem("page-1", "https://example.com/1", "Page 1", "Unchanging content")
    _mock_fetch(monkeypatch, [item])

    await sync_source(source)
    assert len(upsert_calls["upsert"]) == 1

    stats = await sync_source(source)

    assert stats["items_changed"] == 0
    assert len(upsert_calls["upsert"]) == 1, "a second sync with no real change re-embedded anyway"


async def test_changed_content_is_re_embedded_under_the_same_document(monkeypatch, upsert_calls):
    """A changed page must update the EXISTING Document row, never create
    a second one for what is still the same page."""
    source = await _make_source()
    _mock_fetch(monkeypatch, [FetchedItem("page-1", "https://example.com/1", "Page 1", "Original content")])
    await sync_source(source)
    original_doc = await Document.find_one(Document.source_id == str(source.id))

    _mock_fetch(monkeypatch, [
        FetchedItem("page-1", "https://example.com/1", "Page 1", "Updated content, different text"),
    ])
    stats = await sync_source(source)

    assert stats["items_changed"] == 1
    all_docs = await Document.find(Document.source_id == str(source.id)).to_list()
    assert len(all_docs) == 1, "a changed page created a second Document instead of updating the first"
    assert all_docs[0].id == original_doc.id
    assert len(upsert_calls["delete"]) == 1, "the old vectors for the changed content were never removed"


async def test_only_the_changed_item_is_re_embedded_not_the_whole_source(monkeypatch, upsert_calls):
    source = await _make_source()
    _mock_fetch(monkeypatch, [
        FetchedItem("page-1", "u1", "Page 1", "Content one"),
        FetchedItem("page-2", "u2", "Page 2", "Content two"),
    ])
    await sync_source(source)
    assert len(upsert_calls["upsert"]) == 2

    _mock_fetch(monkeypatch, [
        FetchedItem("page-1", "u1", "Page 1", "Content one"),  # unchanged
        FetchedItem("page-2", "u2", "Page 2", "Content two CHANGED"),  # changed
    ])
    stats = await sync_source(source)

    assert stats["items_changed"] == 1
    assert len(upsert_calls["upsert"]) == 3, "syncing one changed item also re-embedded the unchanged one"


async def test_an_item_no_longer_returned_by_the_source_is_removed(monkeypatch, upsert_calls):
    """A page deleted from the source (Notion, Drive, or taken off a
    website) must have its Document and vectors removed, the same as a
    manual delete."""
    source = await _make_source()
    _mock_fetch(monkeypatch, [
        FetchedItem("page-1", "u1", "Page 1", "Content one"),
        FetchedItem("page-2", "u2", "Page 2", "Content two"),
    ])
    await sync_source(source)

    _mock_fetch(monkeypatch, [FetchedItem("page-1", "u1", "Page 1", "Content one")])  # page-2 is gone
    stats = await sync_source(source)

    assert stats["items_removed"] == 1
    remaining = await Document.find(Document.source_id == str(source.id)).to_list()
    assert {d.external_id for d in remaining} == {"page-1"}
    assert len(upsert_calls["delete"]) == 1


async def test_a_source_that_now_returns_nothing_removes_everything_it_had(monkeypatch, upsert_calls):
    source = await _make_source()
    _mock_fetch(monkeypatch, [FetchedItem("page-1", "u1", "Page 1", "Content")])
    await sync_source(source)

    _mock_fetch(monkeypatch, [])
    stats = await sync_source(source)

    assert stats["items_removed"] == 1
    remaining = await Document.find(Document.source_id == str(source.id)).to_list()
    assert remaining == []


async def test_a_fetch_failure_is_recorded_on_the_source_not_raised(monkeypatch):
    source = await _make_source()

    async def _boom(_source):
        raise ConnectionError("the site is down")

    monkeypatch.setattr("app.services.knowledge_sync._fetch_items", _boom)

    await sync_source(source)  # must not raise

    refreshed = await KnowledgeSource.get(source.id)
    assert refreshed.last_sync_status == "error"
    assert "the site is down" in refreshed.last_sync_error
    assert refreshed.last_synced_at is not None


async def test_a_successful_sync_clears_a_previous_error(monkeypatch, upsert_calls):
    source = await _make_source()
    source.last_sync_status = "error"
    source.last_sync_error = "previous failure"
    await source.save()

    _mock_fetch(monkeypatch, [FetchedItem("page-1", "u1", "Page 1", "Content")])
    await sync_source(source)

    refreshed = await KnowledgeSource.get(source.id)
    assert refreshed.last_sync_status == "ok"
    assert refreshed.last_sync_error == ""


async def test_sync_stats_are_recorded_on_the_source_for_the_ui_to_show(monkeypatch, upsert_calls):
    source = await _make_source()
    _mock_fetch(monkeypatch, [
        FetchedItem("page-1", "u1", "Page 1", "One"),
        FetchedItem("page-2", "u2", "Page 2", "Two"),
    ])
    await sync_source(source)

    refreshed = await KnowledgeSource.get(source.id)
    assert refreshed.last_sync_stats["items_seen"] == 2
    assert refreshed.last_sync_stats["items_changed"] == 2


# ---------------------------------------------------------------------------
# sync_due_sources — the scheduler half
# ---------------------------------------------------------------------------


async def test_a_never_synced_source_is_always_due(monkeypatch, upsert_calls):
    source = await _make_source(sync_interval_minutes=999999)  # effectively "not due for ages"
    _mock_fetch(monkeypatch, [FetchedItem("page-1", "u1", "Page 1", "Content")])

    await sync_due_sources()

    refreshed = await KnowledgeSource.get(source.id)
    assert refreshed.last_synced_at is not None, "a never-synced source was skipped"


async def test_a_source_synced_recently_is_skipped(monkeypatch, upsert_calls):
    source = await _make_source(sync_interval_minutes=60)
    source.last_synced_at = datetime.now(UTC) - timedelta(minutes=5)
    await source.save()
    _mock_fetch(monkeypatch, [FetchedItem("page-1", "u1", "Page 1", "Content")])

    await sync_due_sources()

    assert upsert_calls["upsert"] == [], "a source synced 5 minutes ago with a 60-minute interval ran anyway"


async def test_a_source_past_its_interval_is_synced_again(monkeypatch, upsert_calls):
    source = await _make_source(sync_interval_minutes=60)
    source.last_synced_at = datetime.now(UTC) - timedelta(minutes=90)
    await source.save()
    _mock_fetch(monkeypatch, [FetchedItem("page-1", "u1", "Page 1", "Content")])

    await sync_due_sources()

    assert len(upsert_calls["upsert"]) == 1


async def test_a_disabled_source_is_never_synced(monkeypatch, upsert_calls):
    await _make_source(enabled=False)
    _mock_fetch(monkeypatch, [FetchedItem("page-1", "u1", "Page 1", "Content")])

    await sync_due_sources()

    assert upsert_calls["upsert"] == []


async def test_one_failing_source_does_not_stop_another_from_syncing(monkeypatch, upsert_calls):
    good = await _make_source()
    bad = await _make_source()

    async def _dispatch(source):
        if source.id == bad.id:
            raise ConnectionError("bad source is down")
        return [FetchedItem("page-1", "u1", "Page 1", "Content")]

    monkeypatch.setattr("app.services.knowledge_sync._fetch_items", _dispatch)

    await sync_due_sources()

    good_refreshed = await KnowledgeSource.get(good.id)
    bad_refreshed = await KnowledgeSource.get(bad.id)
    assert good_refreshed.last_sync_status == "ok"
    assert bad_refreshed.last_sync_status == "error"


# ---------------------------------------------------------------------------
# Credential handling for the connectors that need one
# ---------------------------------------------------------------------------


async def test_a_notion_source_with_no_credential_is_skipped_gracefully():
    from app.services.knowledge_sync import _fetch_items

    source = KnowledgeSource(
        bot_id="b1", user_id="u1", kind="notion", config={"page_id": "p1"},
        credential_encrypted="",
    )
    items = await _fetch_items(source)
    assert items == []


async def test_a_google_drive_source_with_no_credential_is_skipped_gracefully():
    from app.services.knowledge_sync import _fetch_items

    source = KnowledgeSource(
        bot_id="b1", user_id="u1", kind="google_drive", config={"folder_id": "f1"},
        credential_encrypted="",
    )
    items = await _fetch_items(source)
    assert items == []


async def test_an_unknown_source_kind_is_skipped_gracefully():
    from app.services.knowledge_sync import _fetch_items

    source = KnowledgeSource(bot_id="b1", user_id="u1", kind="carrier-pigeon", config={})
    items = await _fetch_items(source)
    assert items == []
