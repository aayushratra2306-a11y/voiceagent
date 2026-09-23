import asyncio

import pytest

from app.models.organisation import Membership, Organisation
from app.services.orgs import LastOwnerError, add_member, recount_owner_counts, remove_member, set_role
from tests.conftest import make_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _org(owner_ids: list[str]) -> Organisation:
    org = Organisation(name="Team", created_by=owner_ids[0], owner_count=0)
    await org.insert()
    for uid in owner_ids:
        await add_member(str(org.id), uid, "owner")
    return await Organisation.get(org.id)


async def _count(org) -> int:
    return (await Organisation.get(org.id)).owner_count


async def _real_owners(org) -> int:
    return await Membership.find(Membership.org_id == str(org.id), Membership.role == "owner").count()


# --- the owner-count protocol ------------------------------------------------

async def test_the_last_owner_cannot_be_demoted_or_removed():
    org = await _org(["u1"])
    with pytest.raises(LastOwnerError):
        await set_role(str(org.id), "u1", "admin")
    with pytest.raises(LastOwnerError):
        await remove_member(str(org.id), "u1")
    assert await _count(org) == 1 == await _real_owners(org)


async def test_concurrent_demotions_of_two_owners_leave_one():
    org = await _org(["u2", "u3"])
    results = await asyncio.gather(
        set_role(str(org.id), "u2", "admin"), set_role(str(org.id), "u3", "admin"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, LastOwnerError) for r in results) == 1
    assert await _real_owners(org) == 1 == await _count(org)


async def test_concurrent_promotions_count_once():
    org = await _org(["u4"])
    await add_member(str(org.id), "u5", "member")
    await asyncio.gather(set_role(str(org.id), "u5", "owner"), set_role(str(org.id), "u5", "owner"))
    assert await _real_owners(org) == 2 == await _count(org)


async def test_concurrent_demotions_of_the_same_owner_decrement_once():
    org = await _org(["u6", "u7", "u8"])
    await asyncio.gather(set_role(str(org.id), "u7", "admin"), set_role(str(org.id), "u7", "admin"))
    assert await _real_owners(org) == 2 == await _count(org)


async def test_recount_corrects_drift():
    org = await _org(["u9"])
    await Organisation.get_motor_collection().update_one({"_id": org.id}, {"$set": {"owner_count": 5}})
    assert await recount_owner_counts() >= 1
    assert await _count(org) == 1


# --- the routes ---------------------------------------------------------------

def _h(token, org_id=None):
    h = {"Authorization": f"Bearer {token}"}
    return {**h, "X-Org-Id": org_id} if org_id else h


async def test_get_orgs_lists_my_personal_org(client):
    token = await make_user("orgs-1@voiceagent-test.com")
    orgs = (await client.get("/orgs", headers=_h(token))).json()
    assert len(orgs) == 1 and orgs[0]["personal"] is True and orgs[0]["role"] == "owner"


async def test_get_orgs_heals_a_user_with_no_membership(client):
    token = await make_user("orgs-2@voiceagent-test.com")
    from app.models.user import User
    user = await User.find_one(User.email == "orgs-2@voiceagent-test.com")
    await Membership.find(Membership.user_id == str(user.id)).delete()
    orgs = (await client.get("/orgs", headers=_h(token))).json()
    assert len(orgs) == 1 and orgs[0]["role"] == "owner"


async def test_owner_invites_an_existing_user_who_accepts_and_then_sees_the_org(client):
    # 5.2 — Task 2: /members now invites rather than adding directly (a
    # 201-with-the-added-membership response would have to differ between
    # a known and unknown address, which is the leak this task closes). The
    # invitee only shows up in `viewer`'s orgs once they accept; there is
    # no HTTP accept endpoint yet (a later task), so this calls the
    # invitations service directly, the same way test_invitations_service.py
    # does.
    owner = await make_user("orgs-3@voiceagent-test.com")
    viewer = await make_user("orgs-3v@voiceagent-test.com")
    org_id = (await client.post("/orgs", json={"name": "Acme"}, headers=_h(owner))).json()["id"]

    r = await client.post(
        f"/orgs/{org_id}/members", json={"email": "orgs-3v@voiceagent-test.com", "role": "viewer"}, headers=_h(owner)
    )
    assert r.status_code == 202
    assert r.json()["status"] == "invitation sent"

    from app.models.user import User
    from app.services import invitations as inv

    token = r.json()["invite_path"].rsplit("/", 1)[-1]
    viewer_user = await User.find_one(User.email == "orgs-3v@voiceagent-test.com")
    await inv.accept(token, viewer_user)

    names = [o["name"] for o in (await client.get("/orgs", headers=_h(viewer))).json()]
    assert "Acme" in names


async def test_inviting_an_unknown_email_returns_the_same_202_as_a_known_one(client):
    # 5.2 — Task 2 closes the 5.1 leak this test used to assert (a 404 with
    # "No Voix account uses that email" for an unknown address). See
    # test_invitations_api.py::test_inviting_an_unknown_address_looks_identical_to_a_known_one
    # for the side-by-side comparison of both outcomes.
    owner = await make_user("orgs-4@voiceagent-test.com")
    org_id = (await client.post("/orgs", json={"name": "Acme4"}, headers=_h(owner))).json()["id"]
    r = await client.post(
        f"/orgs/{org_id}/members",
        json={"email": "nobody-here@voiceagent-test.com", "role": "member"},
        headers=_h(owner),
    )
    assert r.status_code == 202
    assert r.json()["status"] == "invitation sent"


async def test_an_admin_cannot_touch_an_owner(client):
    owner = await make_user("orgs-5@voiceagent-test.com")
    admin = await make_user("orgs-5a@voiceagent-test.com")
    org_id = (await client.post("/orgs", json={"name": "Acme5"}, headers=_h(owner))).json()["id"]
    from app.models.user import User
    # This test is about the owner-guard on PATCH/DELETE/POST below, not
    # about the invite flow, so the admin membership is added directly
    # (as test_org_role_matrix.py does) rather than through invite+accept.
    admin_id = str((await User.find_one(User.email == "orgs-5a@voiceagent-test.com")).id)
    await add_member(org_id, admin_id, "admin")
    owner_id = str((await User.find_one(User.email == "orgs-5@voiceagent-test.com")).id)

    r1 = await client.patch(f"/orgs/{org_id}/members/{owner_id}", json={"role": "member"}, headers=_h(admin))
    assert r1.status_code == 403
    r2 = await client.delete(f"/orgs/{org_id}/members/{owner_id}", headers=_h(admin))
    assert r2.status_code == 403
    r3 = await client.post(
        f"/orgs/{org_id}/members", json={"email": "orgs-5@voiceagent-test.com", "role": "owner"}, headers=_h(admin)
    )
    assert r3.status_code == 403


async def test_the_last_owner_cannot_leave_but_a_viewer_can(client):
    owner = await make_user("orgs-6@voiceagent-test.com")
    viewer = await make_user("orgs-6v@voiceagent-test.com")
    org_id = (await client.post("/orgs", json={"name": "Acme6"}, headers=_h(owner))).json()["id"]
    from app.models.user import User
    # Membership setup, not the invite flow under test elsewhere — added
    # directly, same as test_an_admin_cannot_touch_an_owner above.
    viewer_id = str((await User.find_one(User.email == "orgs-6v@voiceagent-test.com")).id)
    await add_member(org_id, viewer_id, "viewer")
    owner_id = str((await User.find_one(User.email == "orgs-6@voiceagent-test.com")).id)

    assert (await client.delete(f"/orgs/{org_id}/members/{owner_id}", headers=_h(owner))).status_code == 409
    assert (await client.delete(f"/orgs/{org_id}/members/{viewer_id}", headers=_h(viewer))).status_code == 204


async def test_an_admin_of_a_cannot_rename_b_by_sending_as_header(client):
    a = await make_user("orgs-7@voiceagent-test.com")
    b = await make_user("orgs-7b@voiceagent-test.com")
    org_a = (await client.post("/orgs", json={"name": "A7"}, headers=_h(a))).json()["id"]
    org_b = (await client.post("/orgs", json={"name": "B7"}, headers=_h(b))).json()["id"]

    r = await client.patch(f"/orgs/{org_b}", json={"name": "pwned"}, headers=_h(a, org_a))
    assert r.status_code == 404
    names = [o["name"] for o in (await client.get("/orgs", headers=_h(b))).json()]
    assert "B7" in names and "pwned" not in names


async def test_only_an_empty_org_that_is_not_your_last_can_be_deleted(client):
    owner = await make_user("orgs-8@voiceagent-test.com")
    org_id = (await client.post("/orgs", json={"name": "Acme8"}, headers=_h(owner))).json()["id"]
    r = await client.post("/bots/", json={"name": "keeps it busy"}, headers=_h(owner, org_id))
    assert r.status_code == 201
    assert (await client.delete(f"/orgs/{org_id}", headers=_h(owner))).status_code == 409
    await client.delete(f"/bots/{r.json()['id']}", headers=_h(owner, org_id))
    assert (await client.delete(f"/orgs/{org_id}", headers=_h(owner))).status_code == 204

    only = (await client.get("/orgs", headers=_h(owner))).json()
    assert len(only) == 1
    assert (await client.delete(f"/orgs/{only[0]['id']}", headers=_h(owner))).status_code == 409
