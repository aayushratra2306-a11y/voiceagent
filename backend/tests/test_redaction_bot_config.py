"""Task 6.2 — the bot API side: a customer can see and change which
categories are redacted, a typo'd category is rejected rather than
silently ignored, and a new bot defaults to the safe answer.
"""

import pytest

from tests.conftest import auth_headers

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _create(client, token, **fields):
    resp = await client.post("/bots/", json={"name": "Redaction probe", **fields},
                              headers=auth_headers(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _listed(client, token, bot_id):
    body = (await client.get("/bots/", headers=auth_headers(token))).json()
    return next(b for b in body if b["id"] == bot_id)


async def test_a_new_bot_defaults_to_redacting_everything(client, user_a_token):
    """An operator who has not thought about this gets the safe answer —
    every category, not an empty do-nothing list."""
    bot_id = await _create(client, user_a_token)
    bot = await _listed(client, user_a_token, bot_id)

    assert set(bot["redact_transcripts"]) == {
        "card", "cvv", "aadhaar", "pan", "phone", "email", "spoken_digits",
    }


async def test_a_customer_can_narrow_which_categories_are_redacted(client, user_a_token):
    bot_id = await _create(client, user_a_token, redact_transcripts=["card", "cvv"])
    bot = await _listed(client, user_a_token, bot_id)
    assert bot["redact_transcripts"] == ["card", "cvv"]


async def test_a_typo_in_a_category_name_is_rejected_not_silently_dropped(client, user_a_token):
    """A dropped typo would leave a customer believing card numbers are
    redacted when nothing matches that name at all — worse than refusing
    outright."""
    resp = await client.post(
        "/bots/", json={"name": "Bad category bot", "redact_transcripts": ["crd"]},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 422


async def test_a_customer_can_turn_redaction_off_entirely(client, user_a_token):
    """Explicit, deliberate, and different from never having set it — the
    API allows it because it is the customer's own compliance decision to
    make, not this platform's to prevent."""
    bot_id = await _create(client, user_a_token, redact_transcripts=[])
    bot = await _listed(client, user_a_token, bot_id)
    assert bot["redact_transcripts"] == []


async def test_the_setting_can_be_changed_after_creation(client, user_a_token):
    bot_id = await _create(client, user_a_token)

    resp = await client.patch(
        f"/bots/{bot_id}", json={"redact_transcripts": ["email"]},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 200

    bot = await _listed(client, user_a_token, bot_id)
    assert bot["redact_transcripts"] == ["email"]


async def test_updating_with_a_typo_is_also_rejected(client, user_a_token):
    bot_id = await _create(client, user_a_token)
    resp = await client.patch(
        f"/bots/{bot_id}", json={"redact_transcripts": ["not-a-real-kind"]},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 422
