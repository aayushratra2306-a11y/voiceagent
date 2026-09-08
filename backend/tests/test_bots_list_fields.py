"""Guards that GET /bots/ returns everything the editor needs.

FOUND 2026-09-04, reported as "under Hindi one I can only see Sarah and
Sierra" -- the English voices -- on a bot whose language is Hindi.

GET /bots/ returned only id, name, llm_model and voice_id. BotSettingsPage
has no other way to read a bot: it loads one by finding it in this list. So
`bot.language` arrived undefined, `voicesFor(undefined)` fell through to the
English list, and the form showed English voices for a Hindi bot. The system
prompt box was quietly empty for the same reason.

Nothing caught it because it was harmless right up until it wasn't: while
voices were a single fixed English list, no part of the page depended on the
language. dc2bdd5 made voices per-language and the omission became visible.

Saving from that state did not corrupt the stored values, but only because
update_bot drops None fields -- luck, not design, and not something to rely
on. These tests pin the contract instead: the editor's fields and the API's
fields are the same set.
"""

import pytest

from tests.conftest import auth_headers

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Exactly what BotSettingsPage's form holds (its DEFAULTS object), plus the id
# it needs to find the bot at all.
EDITOR_FIELDS = {"id", "name", "system_prompt", "voice_id", "llm_model", "language"}


async def _create(client, token, **fields):
    resp = await client.post("/bots/", json=fields, headers=auth_headers(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _listed(client, token, name):
    body = (await client.get("/bots/", headers=auth_headers(token))).json()
    return next(b for b in body if b["name"] == name)


async def test_list_returns_every_field_the_editor_needs(client, user_a_token):
    await _create(
        client, user_a_token,
        name="Fields probe", system_prompt="You are a helpful voice assistant.",
        voice_id="6b02ffe5-e3cb-48c0-a023-c72f85953375",
        llm_model="gpt-4o-mini", language="hi",
    )
    bot = await _listed(client, user_a_token, "Fields probe")
    missing = EDITOR_FIELDS - set(bot)
    assert not missing, (
        f"GET /bots/ omits {sorted(missing)}; the edit form reads bots from "
        f"this endpoint, so anything missing here silently falls back to a "
        f"default in the UI"
    )


async def test_a_hindi_bots_language_actually_comes_back(client, user_a_token):
    """The specific regression: language present and correct, not defaulted.

    An 'en' here is indistinguishable in the UI from a missing value, which is
    exactly why the original bug looked like a frontend problem.
    """
    await _create(client, user_a_token, name="Guddu-Ji", language="hi")
    bot = await _listed(client, user_a_token, "Guddu-Ji")
    assert bot["language"] == "hi", (
        "a Hindi bot reports as English, so the editor offers English voices"
    )


async def test_the_system_prompt_comes_back_too(client, user_a_token):
    """Also missing, and worse in a quiet way: the editor showed an empty
    prompt box for a bot that had one."""
    prompt = "Hi, i am Nitya, how can i help you today"
    await _create(client, user_a_token, name="Prompted", system_prompt=prompt)
    bot = await _listed(client, user_a_token, "Prompted")
    assert bot["system_prompt"] == prompt


async def test_other_users_bots_are_still_not_listed(client, user_a_token, user_b_token):
    """Widening the response must not widen who can see it."""
    await _create(client, user_b_token, name="B's private bot", language="hi")
    names = [
        b["name"]
        for b in (await client.get("/bots/", headers=auth_headers(user_a_token))).json()
    ]
    assert "B's private bot" not in names
