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

    # The fourth element is the body app/core/org.py's binding constraint
    # promises for THIS attempt when it is made with the caller's OWN,
    # genuine organisation header (auth_headers(a)): the org membership
    # check passes, so the request reaches the route's own by-id-and-org_id
    # lookup, and that lookup's own "not found" message is what comes back
    # — never "Organisation not found", because the organisation itself was
    # found and it was the resource inside it that wasn't. Verified against
    # the real routes rather than assumed: fetch_org_bot (app/core/org.py)
    # raises "Bot not found" for every /bots/{bot_id}... path (bot_tools.py
    # and documents.py resolve the bot the same way before ever looking at
    # a tool_id or doc_id), and webhooks.py's _owned_subscription raises
    # "Subscription not found".
    new_tool = {"name": "t2", "description": "d", "url": "https://api.example.com/z"}
    updated_tool = {"name": "t3", "description": "d", "url": "https://api.example.com/z"}
    updated_hook = {"event": "call.ended", "url": "https://hooks.example.com/q"}
    attempts = [
        ("patch", f"/bots/{bot_b}", {"name": "x"}, "Bot not found"),
        ("delete", f"/bots/{bot_b}", None, "Bot not found"),
        ("get", f"/bots/{bot_b}/tools/", None, "Bot not found"),
        ("post", f"/bots/{bot_b}/tools/", new_tool, "Bot not found"),
        ("patch", f"/bots/{bot_b}/tools/{tool_b}", updated_tool, "Bot not found"),
        ("delete", f"/bots/{bot_b}/tools/{tool_b}", None, "Bot not found"),
        ("post", f"/bots/{bot_b}/tools/{tool_b}/test", {}, "Bot not found"),
        ("get", f"/bots/{bot_b}/documents", None, "Bot not found"),
        ("patch", f"/webhooks/{hook_b}", updated_hook, "Subscription not found"),
        ("delete", f"/webhooks/{hook_b}", None, "Subscription not found"),
        ("get", f"/webhooks/{hook_b}/deliveries", None, "Subscription not found"),
        ("post", f"/webhooks/{hook_b}/test", None, "Subscription not found"),
        ("post", "/connect", {"bot_id": bot_b, "sdp": "v=0", "type": "offer"}, "Bot not found"),
    ]
    # Binding constraint order is 401 -> 400 -> 404 "Organisation not
    # found" -> 403 -> another org's resource -> 404. The two headers below
    # land on different rungs of that ladder for the SAME request: A's own
    # header names A's own org, where A is a genuine member, so the org
    # check passes and the 404 comes from the resource lookup inside it
    # (the resource-specific message above). A's token with B's org id in
    # the header never gets that far — A is not a member of B, so
    # get_org_context's own membership check is what 404s, and every
    # attempt gets that same "Organisation not found" body regardless of
    # which route it is, because none of them are reached.
    for header, expect_detail in (
        (auth_headers(a), None),  # None means "use the per-attempt detail below"
        (org_headers(a, _org_of_token[b]), "Organisation not found"),
    ):
        for method, path, body, own_org_detail in attempts:
            kwargs = {"headers": header}
            if body is not None:
                kwargs["json"] = body
            r = await getattr(client, method)(path, **kwargs)
            org = header.get("X-Org-Id")
            wanted = expect_detail or own_org_detail
            assert r.status_code == 404, f"{method.upper()} {path} with {org}: {r.status_code} {r.text}"
            assert r.json()["detail"] == wanted, f"{method.upper()} {path} with {org}: {r.text}"


async def test_bot_lists_never_mix(client):
    a, bot_a, _, _ = await _world(client, "iso-c@voiceagent-test.com")
    b, bot_b, _, _ = await _world(client, "iso-d@voiceagent-test.com")
    ids_a = {x["id"] for x in (await client.get("/bots/", headers=auth_headers(a))).json()}
    assert bot_a in ids_a and bot_b not in ids_a
