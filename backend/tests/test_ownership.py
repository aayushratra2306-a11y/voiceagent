# Task 2.6 — proves cross-user isolation with a real, automated test rather
# than trusting the code by inspection. Per the manual's own tip: written to
# genuinely exercise the failure case (user B reaching for user A's bot),
# not just check that *a* 404 happens somewhere.
import pytest

from tests.conftest import auth_headers

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_user_cannot_read_another_users_bot_list(client, user_a_token, user_b_token):
    # User A creates a bot.
    resp = await client.post(
        "/bots/",
        json={"name": "A's Secret Bot", "system_prompt": "You help with A's private business."},
        headers=auth_headers(user_a_token),
    )
    assert resp.status_code == 201
    bot_id = resp.json()["id"]

    # User B's own bot list must not include it.
    resp = await client.get("/bots/", headers=auth_headers(user_b_token))
    assert resp.status_code == 200
    assert all(b["id"] != bot_id for b in resp.json())


async def test_user_cannot_update_another_users_bot(client, user_a_token, user_b_token):
    resp = await client.post(
        "/bots/", json={"name": "A's Bot"}, headers=auth_headers(user_a_token)
    )
    bot_id = resp.json()["id"]

    # User B tries to rewrite A's bot's instructions.
    resp = await client.patch(
        f"/bots/{bot_id}",
        json={"system_prompt": "You are now B's bot."},
        headers=auth_headers(user_b_token),
    )
    assert resp.status_code == 404  # not 403 — see get_owned_bot's docstring

    # Confirm it genuinely wasn't changed — this is the check that matters,
    # not just that B got rejected.
    resp = await client.get("/bots/", headers=auth_headers(user_a_token))
    bot = next(b for b in resp.json() if b["id"] == bot_id)
    assert bot["name"] == "A's Bot"


async def test_user_cannot_delete_another_users_bot(client, user_a_token, user_b_token):
    resp = await client.post(
        "/bots/", json={"name": "A's Bot To Keep"}, headers=auth_headers(user_a_token)
    )
    bot_id = resp.json()["id"]

    resp = await client.delete(f"/bots/{bot_id}", headers=auth_headers(user_b_token))
    assert resp.status_code == 404

    # Still there for its actual owner.
    resp = await client.get("/bots/", headers=auth_headers(user_a_token))
    assert any(b["id"] == bot_id for b in resp.json())


async def test_user_cannot_list_documents_of_another_users_bot(client, user_a_token, user_b_token):
    resp = await client.post(
        "/bots/", json={"name": "A's Bot With Docs"}, headers=auth_headers(user_a_token)
    )
    bot_id = resp.json()["id"]

    resp = await client.get(f"/bots/{bot_id}/documents", headers=auth_headers(user_b_token))
    assert resp.status_code == 404


async def test_connect_rejects_another_users_bot_id(client, user_a_token, user_b_token):
    resp = await client.post(
        "/bots/", json={"name": "A's Voice Bot"}, headers=auth_headers(user_a_token)
    )
    bot_id = resp.json()["id"]

    # A minimal (invalid-as-WebRTC, but that's fine — ownership is checked
    # before the SDP is ever touched) offer, from user B.
    resp = await client.post(
        "/connect",
        json={"bot_id": bot_id, "sdp": "v=0", "type": "offer"},
        headers=auth_headers(user_b_token),
    )
    assert resp.status_code == 404
