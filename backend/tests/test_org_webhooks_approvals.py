import pytest

from app.models.approval import PendingApproval
from app.models.bot_tool import BotTool
from app.models.webhook import WebhookOutboxItem, WebhookSubscription
from app.services.webhooks import emit
from tests.conftest import _org_of_token, auth_headers, make_user, org_headers

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_emit_reaches_the_organisations_subscriptions_only(client):
    a = await make_user("wh-a@voiceagent-test.com")
    b = await make_user("wh-b@voiceagent-test.com")
    for token in (a, b):
        r = await client.post(
            "/webhooks/", json={"event": "call.ended", "url": "https://hooks.example.com/x"},
            headers=auth_headers(token),
        )
        assert r.status_code == 201, r.text

    await emit("call.ended", org_id=_org_of_token[a], payload={"k": 1})

    subs_a = {
        str(s.id)
        for s in await WebhookSubscription.find(WebhookSubscription.org_id == _org_of_token[a]).to_list()
    }
    queued = {i.subscription_id for i in await WebhookOutboxItem.find(WebhookOutboxItem.payload == {"k": 1}).to_list()}
    assert queued and queued <= subs_a


async def test_a_member_cannot_see_webhooks_or_approvals(client):
    from app.models.organisation import Membership
    from app.models.user import User
    owner = await make_user("wh-o@voiceagent-test.com")
    member = await make_user("wh-m@voiceagent-test.com")
    mid = str((await User.find_one(User.email == "wh-m@voiceagent-test.com")).id)
    org = _org_of_token[owner]
    await Membership(org_id=org, user_id=mid, role="member").insert()

    assert (await client.get("/webhooks/", headers=org_headers(member, org))).status_code == 403
    assert (await client.get("/approvals/pending-count", headers=org_headers(member, org))).status_code == 403


async def test_approve_refuses_a_tool_from_another_org(client):
    a = await make_user("ap-a@voiceagent-test.com")
    b = await make_user("ap-b@voiceagent-test.com")
    foreign_tool = BotTool(bot_id="x", org_id=_org_of_token[b], name="refund", description="d", url="https://api.example.com/r")
    await foreign_tool.insert()
    approval = PendingApproval(
        tool_id=str(foreign_tool.id), bot_id="x", user_id="u", org_id=_org_of_token[a],
        tool_name="refund", arguments={}, amount=10, threshold=5,
    )
    await approval.insert()

    r = await client.post(f"/approvals/{approval.id}/approve", headers=auth_headers(a))
    # approve() raises 409 when the tool no longer resolves (see
    # app/api/approvals.py) — a tool belonging to another org must land in
    # that exact "tool no longer exists" path, not a different error.
    assert r.status_code == 409, r.text
    assert (await PendingApproval.get(approval.id)).executed is False
