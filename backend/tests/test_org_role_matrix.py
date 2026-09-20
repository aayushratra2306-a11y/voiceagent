"""The spec's role table, checked cell by cell."""

import pytest

from app.models.organisation import Membership
from app.models.user import User
from tests.conftest import _org_of_token, make_user, org_headers

pytestmark = pytest.mark.asyncio(loop_scope="session")

# (method, path-template, body, minimum role)
ACTIONS = [
    ("get", "/bots/", None, "viewer"),
    ("post", "/bots/", {"name": "m"}, "member"),
    ("get", "/webhooks/", None, "admin"),
    ("get", "/approvals/pending-count", None, "admin"),
    ("get", "/orgs/{org}/members", None, "viewer"),
    ("patch", "/orgs/{org}", {"name": "renamed"}, "admin"),
]
RANK = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}


@pytest.mark.parametrize("role", ["viewer", "member", "admin"])
async def test_each_role_can_do_exactly_what_the_table_says(client, role):
    owner = await make_user(f"matrix-owner-{role}@voiceagent-test.com")
    org = _org_of_token[owner]
    token = await make_user(f"matrix-{role}@voiceagent-test.com")
    uid = str((await User.find_one(User.email == f"matrix-{role}@voiceagent-test.com")).id)
    await Membership(org_id=org, user_id=uid, role=role).insert()

    for method, path, body, minimum in ACTIONS:
        kwargs = {"headers": org_headers(token, org)}
        if body is not None:
            kwargs["json"] = body
        r = await getattr(client, method)(path.format(org=org), **kwargs)
        allowed = RANK[role] >= RANK[minimum]
        assert (r.status_code < 400) == allowed, f"{role} {method.upper()} {path}: {r.status_code} {r.text}"
        if not allowed:
            assert r.status_code == 403
