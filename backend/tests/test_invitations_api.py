"""Task 5.2 (Task 2) — the HTTP endpoints an admin uses to invite, list
pending invitations, and revoke one.

The point of this file: POST /orgs/{org_id}/members used to leak whether an
email address had a Voix account (a 404 for unknown addresses, a 201 for
known ones). It now always returns the same 202, whatever the address is —
that is the hardest path here, tested first below. list/revoke are tested
for org-scoping (BOLA), per the global constraint that a resource is fetched
by id AND org_id in one query, never fetched-then-compared.
"""

import pytest

from app.models.organisation import Membership
from app.models.user import User
from app.services import invitations as inv
from tests.conftest import _org_of_token, make_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _bearer(token: str) -> dict:
    # Deliberately NOT sending X-Org-Id here: every route this file
    # exercises is require_role(..., from_path=True), so the path alone
    # names the organisation (see app/core/org.py's get_org_context_from_path).
    # A cross-org test that also sent a matching X-Org-Id header would
    # short-circuit through the header-mismatch check instead of the real
    # "not a member of that org" path it means to exercise.
    return {"Authorization": f"Bearer {token}"}


async def _add_membership(org_id: str, email: str, role: str) -> str:
    """A user in `org_id` with `role`, added directly through Membership —
    bypassing the invite HTTP flow, which is the thing under test here (see
    tests/test_org_role_matrix.py for the same pattern). Returns their
    access token."""
    token = await make_user(email)
    uid = str((await User.find_one(User.email == email)).id)
    await Membership(org_id=org_id, user_id=uid, role=role).insert()
    return token


async def _org_with_admin_and_member(suffix: str):
    """A fresh org (owned by a throwaway user), plus an admin token and a
    member token already inside it."""
    owner = await make_user(f"inv-owner-{suffix}@voiceagent-test.com")
    org = _org_of_token[owner]
    admin = await _add_membership(org, f"inv-admin-{suffix}@voiceagent-test.com", "admin")
    member = await _add_membership(org, f"inv-member-{suffix}@voiceagent-test.com", "member")
    return org, admin, member


# --- the leak this task closes -------------------------------------------------


async def test_inviting_an_unknown_address_looks_identical_to_a_known_one(client):
    """The 5.1 enumeration leak. Both answers must match exactly."""
    org, admin, _ = await _org_with_admin_and_member("u1")
    other_org, _, _ = await _org_with_admin_and_member("u1-other")
    known_email = "known-u1@voiceagent-test.com"
    known_token = await make_user(known_email)
    known_uid = str((await User.find_one(User.email == known_email)).id)
    await Membership(org_id=other_org, user_id=known_uid, role="member").insert()
    assert known_token  # the account genuinely exists (elsewhere), unused otherwise

    a = await client.post(
        f"/orgs/{org}/members", json={"email": known_email, "role": "member"}, headers=_bearer(admin)
    )
    b = await client.post(
        f"/orgs/{org}/members", json={"email": "nobody-u1@voiceagent-test.com", "role": "member"},
        headers=_bearer(admin),
    )
    assert a.status_code == b.status_code == 202
    assert a.json().keys() == b.json().keys()
    assert a.json()["status"] == b.json()["status"] == "invitation sent"


async def test_inviting_an_existing_member_returns_the_identical_202(client):
    """Inviting someone already in the org is handled by the accept step,
    not here — a differing response here would itself leak "this address
    is already a member of this org"."""
    org, admin, _ = await _org_with_admin_and_member("u7")
    r = await client.post(
        f"/orgs/{org}/members", json={"email": "inv-member-u7@voiceagent-test.com", "role": "member"},
        headers=_bearer(admin),
    )
    assert r.status_code == 202
    assert r.json()["status"] == "invitation sent"
    assert set(r.json().keys()) == {"status", "invite_path", "expires_at"}


# --- role gates (reused from app/core/org.py, not reimplemented) ---------------


async def test_a_member_cannot_invite(client):
    org, _, member = await _org_with_admin_and_member("u2")
    r = await client.post(
        f"/orgs/{org}/members", json={"email": "x-u2@voiceagent-test.com", "role": "member"}, headers=_bearer(member)
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "Your role (member) can't do this"


async def test_only_an_owner_can_invite_an_owner(client):
    org, admin, _ = await _org_with_admin_and_member("u3")
    r = await client.post(
        f"/orgs/{org}/members", json={"email": "x-u3@voiceagent-test.com", "role": "owner"}, headers=_bearer(admin)
    )
    assert r.status_code == 403


async def test_a_member_cannot_list_or_revoke_invitations(client):
    org, admin, member = await _org_with_admin_and_member("u10")
    r = await client.post(
        f"/orgs/{org}/members", json={"email": "x-u10@voiceagent-test.com", "role": "member"}, headers=_bearer(admin)
    )
    invite_id = (await inv.find_valid(r.json()["invite_path"].rsplit("/", 1)[-1])).id

    r1 = await client.get(f"/orgs/{org}/invitations", headers=_bearer(member))
    assert r1.status_code == 403

    r2 = await client.delete(f"/orgs/{org}/invitations/{invite_id}", headers=_bearer(member))
    assert r2.status_code == 403


# --- org-scoping (BOLA) ---------------------------------------------------------


async def test_listing_an_org_you_do_not_belong_to_is_a_404(client):
    """Stops at the org dependency, BEFORE list_pending runs — so this does
    NOT prove the query itself is org-scoped. That is
    test_list_returns_only_this_orgs_pending_invitations' job; keeping the
    two separate was a review finding on this file."""
    org, admin, _ = await _org_with_admin_and_member("u4")
    other_org, _, _ = await _org_with_admin_and_member("u4-other")
    r = await client.get(f"/orgs/{other_org}/invitations", headers=_bearer(admin))
    assert r.status_code == 404
    assert r.json()["detail"] == "Organisation not found"


async def test_revoking_another_orgs_invitation_is_a_404(client):
    org, admin, _ = await _org_with_admin_and_member("u5")
    other_org, _, _ = await _org_with_admin_and_member("u5-other")
    invite, _ = await inv.create_invitation(other_org, "x-u5@voiceagent-test.com", "member", "u1")
    r = await client.delete(f"/orgs/{org}/invitations/{invite.id}", headers=_bearer(admin))
    assert r.status_code == 404


async def test_list_returns_only_this_orgs_pending_invitations(client):
    org, admin, _ = await _org_with_admin_and_member("u8")
    other_org, other_admin, _ = await _org_with_admin_and_member("u8-other")
    await client.post(
        f"/orgs/{org}/members", json={"email": "in-u8@voiceagent-test.com", "role": "member"}, headers=_bearer(admin)
    )
    await client.post(
        f"/orgs/{other_org}/members", json={"email": "out-u8@voiceagent-test.com", "role": "member"},
        headers=_bearer(other_admin),
    )

    r = await client.get(f"/orgs/{org}/invitations", headers=_bearer(admin))
    assert r.status_code == 200
    emails = [i["email"] for i in r.json()]
    assert emails == ["in-u8@voiceagent-test.com"]
    assert set(r.json()[0].keys()) == {"id", "email", "role", "invited_at", "expires_at"}


async def test_revoke_removes_it_from_the_pending_list(client):
    org, admin, _ = await _org_with_admin_and_member("u9")
    r = await client.post(
        f"/orgs/{org}/members", json={"email": "rev-u9@voiceagent-test.com", "role": "member"}, headers=_bearer(admin)
    )
    token = r.json()["invite_path"].rsplit("/", 1)[-1]
    invite = await inv.find_valid(token)

    d = await client.delete(f"/orgs/{org}/invitations/{invite.id}", headers=_bearer(admin))
    assert d.status_code == 204

    emails = [i["email"] for i in (await client.get(f"/orgs/{org}/invitations", headers=_bearer(admin))).json()]
    assert "rev-u9@voiceagent-test.com" not in emails


# --- the invite path is real ----------------------------------------------------


async def test_the_returned_path_actually_works(client):
    org, admin, _ = await _org_with_admin_and_member("u6")
    r = await client.post(
        f"/orgs/{org}/members", json={"email": "x-u6@voiceagent-test.com", "role": "member"}, headers=_bearer(admin)
    )
    token = r.json()["invite_path"].rsplit("/", 1)[-1]
    assert (await inv.find_valid(token)).email == "x-u6@voiceagent-test.com"


# --- review round 1: revoking is an owner-touching action too ------------------

async def test_an_admin_cannot_revoke_an_owner_invitation(client):
    """Every other owner-touching action in orgs.py is guarded (add, remove,
    promote, demote). Cancelling an owner's succession plan is one too: an
    admin ranks below an owner and must not undo it unilaterally."""
    org, admin, _ = await _org_with_admin_and_member("rev1")
    invite, _ = await inv.create_invitation(org, "heir-rev1@voiceagent-test.com", "owner", "u1")
    r = await client.delete(f"/orgs/{org}/invitations/{invite.id}", headers=_bearer(admin))
    assert r.status_code == 403
    assert r.json()["detail"] == "Only an owner can change another owner"
    assert len(await inv.list_pending(org)) == 1


async def test_an_owner_can_revoke_an_owner_invitation(client):
    owner_token = await make_user("inv-owner-rev2@voiceagent-test.com")
    org = _org_of_token[owner_token]
    invite, _ = await inv.create_invitation(org, "heir-rev2@voiceagent-test.com", "owner", "u1")
    r = await client.delete(f"/orgs/{org}/invitations/{invite.id}", headers=_bearer(owner_token))
    assert r.status_code == 204
    assert await inv.list_pending(org) == []


async def test_revoking_an_already_revoked_invitation_is_a_404(client):
    org, admin, _ = await _org_with_admin_and_member("rev3")
    invite, _ = await inv.create_invitation(org, "x-rev3@voiceagent-test.com", "member", "u1")
    first = await client.delete(f"/orgs/{org}/invitations/{invite.id}", headers=_bearer(admin))
    assert first.status_code == 204
    again = await client.delete(f"/orgs/{org}/invitations/{invite.id}", headers=_bearer(admin))
    assert again.status_code == 404


async def test_revoking_an_accepted_invitation_is_a_404(client):
    org, admin, _ = await _org_with_admin_and_member("rev4")
    email = "joined-rev4@voiceagent-test.com"
    invite, raw = await inv.create_invitation(org, email, "member", "u1")
    await make_user(email)
    await inv.accept(raw, await User.find_one(User.email == email))
    r = await client.delete(f"/orgs/{org}/invitations/{invite.id}", headers=_bearer(admin))
    assert r.status_code == 404
