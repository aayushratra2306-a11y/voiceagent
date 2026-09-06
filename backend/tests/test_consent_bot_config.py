"""Task 6.3 — the bot API side: recording on/off, editable consent
wording, and a validated retention period.
"""

import pytest

from tests.conftest import auth_headers

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _create(client, token, **fields):
    resp = await client.post("/bots/", json={"name": "Consent probe", **fields},
                              headers=auth_headers(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _listed(client, token, bot_id):
    body = (await client.get("/bots/", headers=auth_headers(token))).json()
    return next(b for b in body if b["id"] == bot_id)


async def test_a_new_bot_records_by_default_with_a_generic_disclosure(client, user_a_token):
    """Matches what every bot has always done since task 1.5 (transcripts
    are saved) — the new behaviour is disclosure, not a new decision to
    start recording."""
    bot_id = await _create(client, user_a_token)
    bot = await _listed(client, user_a_token, bot_id)

    assert bot["recording_enabled"] is True
    assert "recorded" in bot["consent_announcement"].lower()
    assert bot["recording_retention_days"] == 0


async def test_a_customer_can_write_their_own_legal_wording(client, user_a_token):
    """The manual's own tip: legal teams specify exact wording."""
    bot_id = await _create(
        client, user_a_token,
        consent_announcement="Acme Corp records all calls per policy 4.2.",
    )
    bot = await _listed(client, user_a_token, bot_id)
    assert bot["consent_announcement"] == "Acme Corp records all calls per policy 4.2."


async def test_an_overly_long_announcement_is_rejected(client, user_a_token):
    """It is spoken aloud at the start of every call — a document-length
    disclosure has no business there."""
    resp = await client.post(
        "/bots/", json={"name": "Too long", "consent_announcement": "x" * 601},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 422


async def test_recording_can_be_turned_off(client, user_a_token):
    bot_id = await _create(client, user_a_token, recording_enabled=False)
    bot = await _listed(client, user_a_token, bot_id)
    assert bot["recording_enabled"] is False


async def test_a_retention_period_can_be_set(client, user_a_token):
    bot_id = await _create(client, user_a_token, recording_retention_days=90)
    bot = await _listed(client, user_a_token, bot_id)
    assert bot["recording_retention_days"] == 90


async def test_a_negative_retention_period_is_rejected(client, user_a_token):
    resp = await client.post(
        "/bots/", json={"name": "Negative retention", "recording_retention_days": -5},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 422


async def test_settings_can_be_changed_after_creation(client, user_a_token):
    bot_id = await _create(client, user_a_token)

    resp = await client.patch(
        f"/bots/{bot_id}",
        json={"recording_enabled": False, "recording_retention_days": 30},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 200

    bot = await _listed(client, user_a_token, bot_id)
    assert bot["recording_enabled"] is False
    assert bot["recording_retention_days"] == 30
