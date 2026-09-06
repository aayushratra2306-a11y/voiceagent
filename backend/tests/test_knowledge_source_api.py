"""Task 7.5 — the knowledge source API: creation, validation, ownership,
and that a stored credential is genuinely write-only.
"""

import pytest

from tests.conftest import auth_headers

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _create_bot(client, token, name="KS probe bot"):
    resp = await client.post("/bots/", json={"name": name}, headers=auth_headers(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_a_website_source_can_be_created_with_no_credential(client, user_a_token):
    bot_id = await _create_bot(client, user_a_token)
    resp = await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "website", "config": {"start_url": "https://example.com"}},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "website"
    assert body["has_credential"] is False
    assert body["last_sync_status"] == "never"


async def test_an_invalid_kind_is_rejected(client, user_a_token):
    bot_id = await _create_bot(client, user_a_token)
    resp = await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "carrier-pigeon", "config": {}},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 422


async def test_a_website_source_needs_a_real_url(client, user_a_token):
    bot_id = await _create_bot(client, user_a_token)
    resp = await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "website", "config": {"start_url": "not a url"}},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 422


async def test_notion_requires_a_credential(client, user_a_token):
    bot_id = await _create_bot(client, user_a_token)
    resp = await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "notion", "config": {"page_id": "abc123"}},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 422


async def test_notion_requires_exactly_one_of_page_id_or_database_id(client, user_a_token):
    bot_id = await _create_bot(client, user_a_token)

    neither = await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "notion", "config": {}, "credential": "secret-token"},
        headers=auth_headers(user_a_token),
    )
    assert neither.status_code == 422

    both = await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "notion", "config": {"page_id": "a", "database_id": "b"}, "credential": "t"},
        headers=auth_headers(user_a_token),
    )
    assert both.status_code == 422


async def test_a_valid_notion_source_is_created_and_the_credential_is_never_echoed_back(
    client, user_a_token,
):
    bot_id = await _create_bot(client, user_a_token)
    resp = await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "notion", "config": {"page_id": "abc123"}, "credential": "secret_notion_token"},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["has_credential"] is True
    assert "secret_notion_token" not in resp.text
    assert "credential" not in body  # only has_credential (a bool) is returned


async def test_google_drive_requires_a_folder_id(client, user_a_token):
    bot_id = await _create_bot(client, user_a_token)
    resp = await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "google_drive", "config": {}, "credential": "{}"},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 422


async def test_listing_sources_never_leaks_the_credential(client, user_a_token):
    bot_id = await _create_bot(client, user_a_token)
    await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "notion", "config": {"page_id": "p1"}, "credential": "super-secret-value"},
        headers=auth_headers(user_a_token),
    )

    resp = await client.get(f"/bots/{bot_id}/knowledge-sources", headers=auth_headers(user_a_token))
    assert "super-secret-value" not in resp.text


async def test_a_user_cannot_see_another_users_knowledge_sources(client, user_a_token, user_b_token):
    bot_id = await _create_bot(client, user_a_token, name="A's bot")
    await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "website", "config": {"start_url": "https://example.com"}},
        headers=auth_headers(user_a_token),
    )

    resp = await client.get(f"/bots/{bot_id}/knowledge-sources", headers=auth_headers(user_b_token))
    assert resp.status_code == 404  # not their bot at all


async def test_a_user_cannot_patch_another_users_knowledge_source(client, user_a_token, user_b_token):
    bot_id = await _create_bot(client, user_a_token, name="A's second bot")
    create = await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "website", "config": {"start_url": "https://example.com"}},
        headers=auth_headers(user_a_token),
    )
    source_id = create.json()["id"]

    resp = await client.patch(
        f"/knowledge-sources/{source_id}", json={"enabled": False}, headers=auth_headers(user_b_token),
    )
    assert resp.status_code == 404


async def test_updating_the_sync_interval(client, user_a_token):
    bot_id = await _create_bot(client, user_a_token)
    create = await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "website", "config": {"start_url": "https://example.com"}},
        headers=auth_headers(user_a_token),
    )
    source_id = create.json()["id"]

    resp = await client.patch(
        f"/knowledge-sources/{source_id}", json={"sync_interval_minutes": 120},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 200
    assert resp.json()["sync_interval_minutes"] == 120


async def test_disabling_a_source(client, user_a_token):
    bot_id = await _create_bot(client, user_a_token)
    create = await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "website", "config": {"start_url": "https://example.com"}},
        headers=auth_headers(user_a_token),
    )
    source_id = create.json()["id"]

    resp = await client.patch(
        f"/knowledge-sources/{source_id}", json={"enabled": False}, headers=auth_headers(user_a_token),
    )
    assert resp.json()["enabled"] is False


async def test_manual_sync_trigger_updates_the_status(client, user_a_token, monkeypatch):
    bot_id = await _create_bot(client, user_a_token)
    create = await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "website", "config": {"start_url": "https://example.com"}},
        headers=auth_headers(user_a_token),
    )
    source_id = create.json()["id"]

    async def _fake_fetch(_source):
        return []

    monkeypatch.setattr("app.services.knowledge_sync._fetch_items", _fake_fetch)

    resp = await client.post(f"/knowledge-sources/{source_id}/sync", headers=auth_headers(user_a_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_sync_status"] == "ok"
    assert body["this_sync"]["items_seen"] == 0


async def test_deleting_a_source_removes_its_documents_too(client, user_a_token, monkeypatch):
    from app.models.document import Document
    from app.services.knowledge_sources import FetchedItem

    bot_id = await _create_bot(client, user_a_token)
    create = await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "website", "config": {"start_url": "https://example.com"}},
        headers=auth_headers(user_a_token),
    )
    source_id = create.json()["id"]

    async def _fake_fetch(_source):
        return [FetchedItem("page-1", "https://example.com/1", "Page 1", "Some content")]

    async def _fake_upsert(bot_id, doc_id, chunks):
        return len(chunks)

    async def _fake_delete(bot_id, doc_id, chunk_count):
        pass

    monkeypatch.setattr("app.services.knowledge_sync._fetch_items", _fake_fetch)
    monkeypatch.setattr("app.services.knowledge_sync.upsert_document", _fake_upsert)
    monkeypatch.setattr("app.services.knowledge_sync.delete_document_vectors", _fake_delete)
    monkeypatch.setattr("app.api.knowledge_sources.delete_document_vectors", _fake_delete)

    await client.post(f"/knowledge-sources/{source_id}/sync", headers=auth_headers(user_a_token))
    docs_before = await Document.find(Document.source_id == source_id).to_list()
    assert len(docs_before) == 1

    resp = await client.delete(f"/knowledge-sources/{source_id}", headers=auth_headers(user_a_token))
    assert resp.status_code == 204

    docs_after = await Document.find(Document.source_id == source_id).to_list()
    assert docs_after == []


async def test_deleting_a_source_that_isnt_yours_is_refused(client, user_a_token, user_b_token):
    bot_id = await _create_bot(client, user_a_token, name="A's bot for delete test")
    create = await client.post(
        f"/bots/{bot_id}/knowledge-sources",
        json={"kind": "website", "config": {"start_url": "https://example.com"}},
        headers=auth_headers(user_a_token),
    )
    source_id = create.json()["id"]

    resp = await client.delete(f"/knowledge-sources/{source_id}", headers=auth_headers(user_b_token))
    assert resp.status_code == 404
