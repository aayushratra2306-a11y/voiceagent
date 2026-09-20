"""OWASP API1 — a member of A, using B's ids, gets 404 on every route."""

import pytest

from tests.conftest import _org_of_token, auth_headers, make_user, org_headers

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


async def _world(client, email):
    token = await make_user(email)
    h = auth_headers(token)
    bot = (await client.post("/bots/", json={"name": f"{email} bot"}, headers=h)).json()["id"]
    tool = (await client.post(f"/bots/{bot}/tools/", json={
        "name": "lookup", "description": "look it up", "url": "https://api.example.com/x",
    }, headers=h)).json()["id"]
    hook_body = {"event": "call.ended", "url": "https://hooks.example.com/y"}
    hook = (await client.post("/webhooks/", json=hook_body, headers=h)).json()["id"]
    return token, bot, tool, hook


async def test_every_org_scoped_route_refuses_the_other_organisations_ids(client):
    a, _, _, _ = await _world(client, "iso-a@voiceagent-test.com")
    b, bot_b, tool_b, hook_b = await _world(client, "iso-b@voiceagent-test.com")

    attempts = [
        ("patch", f"/bots/{bot_b}", {"name": "x"}),
        ("delete", f"/bots/{bot_b}", None),
        ("get", f"/bots/{bot_b}/tools/", None),
        ("post", f"/bots/{bot_b}/tools/", {"name": "t2", "description": "d", "url": "https://api.example.com/z"}),
        ("patch", f"/bots/{bot_b}/tools/{tool_b}", {"name": "t3", "description": "d", "url": "https://api.example.com/z"}),
        ("delete", f"/bots/{bot_b}/tools/{tool_b}", None),
        ("post", f"/bots/{bot_b}/tools/{tool_b}/test", {}),
        ("get", f"/bots/{bot_b}/documents", None),
        ("patch", f"/webhooks/{hook_b}", {"event": "call.ended", "url": "https://hooks.example.com/q"}),
        ("delete", f"/webhooks/{hook_b}", None),
        ("get", f"/webhooks/{hook_b}/deliveries", None),
        ("post", f"/webhooks/{hook_b}/test", None),
        ("post", "/connect", {"bot_id": bot_b, "sdp": "v=0", "type": "offer"}),
    ]
    for header in (auth_headers(a), org_headers(a, _org_of_token[b])):
        for method, path, body in attempts:
            kwargs = {"headers": header}
            if body is not None:
                kwargs["json"] = body
            r = await getattr(client, method)(path, **kwargs)
            org = header.get("X-Org-Id")
            assert r.status_code == 404, f"{method.upper()} {path} with {org}: {r.status_code} {r.text}"


async def test_bot_lists_never_mix(client):
    a, bot_a, _, _ = await _world(client, "iso-c@voiceagent-test.com")
    b, bot_b, _, _ = await _world(client, "iso-d@voiceagent-test.com")
    ids_a = {x["id"] for x in (await client.get("/bots/", headers=auth_headers(a))).json()}
    assert bot_a in ids_a and bot_b not in ids_a
