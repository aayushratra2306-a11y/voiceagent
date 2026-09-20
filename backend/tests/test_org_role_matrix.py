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
            # Binding constraint: the exact 403 body, not just the code —
            # every ACTIONS row above is gated by app/core/org.py's shared
            # require_role dependency, which raises this one f-string with
            # the caller's OWN role interpolated (never the minimum it
            # failed to meet).
            assert r.json() == {"detail": f"Your role ({role}) can't do this"}


async def test_removing_a_member_enforces_admin_unless_you_are_leaving(client):
    """DELETE /orgs/{org_id}/members/{user_id} (app/api/orgs.py) does not
    gate its admin-vs-self distinction through the require_role dependency
    the way every ACTIONS row above does — it declares only
    `require_role("viewer", from_path=True)` and then checks
    `leaving = user_id == str(ctx.user.id)` by hand inside the function
    body. No dependency-walk or table-driven test above can see that
    branch, so it gets its own direct exercise here: a member removing
    someone else is refused with the same 403 shape as the table above, a
    member removing themselves (leaving) succeeds, and an admin removing
    someone else succeeds too.
    """
    owner = await make_user("matrix-remove-owner@voiceagent-test.com")
    org = _org_of_token[owner]

    member_token = await make_user("matrix-remove-member@voiceagent-test.com")
    member_uid = str((await User.find_one(User.email == "matrix-remove-member@voiceagent-test.com")).id)
    await Membership(org_id=org, user_id=member_uid, role="member").insert()

    await make_user("matrix-remove-bystander@voiceagent-test.com")
    bystander_uid = str((await User.find_one(User.email == "matrix-remove-bystander@voiceagent-test.com")).id)
    await Membership(org_id=org, user_id=bystander_uid, role="viewer").insert()

    # A member removing someone else is refused — the admin check the
    # dependency itself never runs.
    r = await client.delete(f"/orgs/{org}/members/{bystander_uid}", headers=org_headers(member_token, org))
    assert r.status_code == 403
    assert r.json() == {"detail": "Your role (member) can't do this"}

    # The same member removing THEMSELVES (leaving) is allowed regardless
    # of role — that's the whole reason the check is hand-written here
    # instead of left to require_role's flat minimum.
    r = await client.delete(f"/orgs/{org}/members/{member_uid}", headers=org_headers(member_token, org))
    assert r.status_code == 204
    assert r.text == ""

    # An admin removing someone else succeeds.
    admin_token = await make_user("matrix-remove-admin@voiceagent-test.com")
    admin_uid = str((await User.find_one(User.email == "matrix-remove-admin@voiceagent-test.com")).id)
    await Membership(org_id=org, user_id=admin_uid, role="admin").insert()
    r = await client.delete(f"/orgs/{org}/members/{bystander_uid}", headers=org_headers(admin_token, org))
    assert r.status_code == 204
    assert r.text == ""
