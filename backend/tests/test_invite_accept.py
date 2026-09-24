"""Task 5.2 (Task 3) — the invitee side: GET /invitations/{token} (no auth,
a preview) and POST /invitations/{token}/accept (auth required, joins the
org).

The highest-value cases here are not the happy path: they're that every
unusable token — unknown, expired, revoked, already-accepted — answers
identically on the unauthenticated GET (anyone on the internet can probe
it), and that accepting cannot ever produce two memberships.

Email suffixes are unique to this file ("ia-...-<n>"), per conftest's
make_user reuse-by-email trap: a suffix shared with another test file would
collide on the one_membership_per_user_per_org index.
"""

from datetime import UTC, datetime, timedelta

import pytest
from beanie import PydanticObjectId

from app.models.organisation import Membership
from app.models.user import User
from app.services import invitations as inv
from app.services import orgs as org_service
from tests.conftest import _org_of_token, make_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _uid(email: str) -> str:
    return str((await User.find_one(User.email == email)).id)


async def _org_with_owner(suffix: str):
    """A fresh org (the throwaway owner's personal org) and that owner's
    user id, for use as the inviter."""
    owner_email = f"ia-owner-{suffix}@voiceagent-test.com"
    owner_token = await make_user(owner_email)
    org = _org_of_token[owner_token]
    return org, await _uid(owner_email)


# --- GET /invitations/{token} — the unauthenticated preview --------------------


async def test_the_preview_needs_no_login(client):
    suffix = "prev1"
    org, owner_id = await _org_with_owner(suffix)
    invitee_email = f"ia-invitee-{suffix}@voiceagent-test.com"
    invite, raw = await inv.create_invitation(org, invitee_email, "member", owner_id)

    r = await client.get(f"/invitations/{raw}")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"org_name", "role", "email", "invited_by_email"}
    assert body["role"] == "member"
    assert body["email"] == invitee_email
    assert body["invited_by_email"] == f"ia-owner-{suffix}@voiceagent-test.com"


async def test_every_bad_token_answers_identically(client):
    """Unknown, expired and revoked must be indistinguishable."""
    suffix = "bad1"
    org, owner_id = await _org_with_owner(suffix)

    unknown = "this-token-was-never-issued-00000000000000000000"

    expired_invite, expired_raw = await inv.create_invitation(
        org, f"ia-expired-{suffix}@voiceagent-test.com", "member", owner_id
    )
    expired_invite.expires_at = datetime.now(UTC) - timedelta(days=1)
    await expired_invite.save()

    revoked_invite, revoked_raw = await inv.create_invitation(
        org, f"ia-revoked-{suffix}@voiceagent-test.com", "member", owner_id
    )
    assert await inv.revoke(org, str(revoked_invite.id))

    answers = [await client.get(f"/invitations/{t}") for t in (unknown, expired_raw, revoked_raw)]
    assert {r.status_code for r in answers} == {404}
    assert len({r.json()["detail"] for r in answers}) == 1


async def test_an_already_accepted_token_answers_the_same_404_on_preview(client):
    """A fourth unusable reason, not in the brief's sample trio: once
    accepted, the token must look exactly as unusable as the others to an
    unauthenticated GET."""
    suffix = "used1"
    org, owner_id = await _org_with_owner(suffix)
    invitee_email = f"ia-used-{suffix}@voiceagent-test.com"
    invite, raw = await inv.create_invitation(org, invitee_email, "member", owner_id)
    invitee_token = await make_user(invitee_email)

    accept = await client.post(f"/invitations/{raw}/accept", headers=_bearer(invitee_token))
    assert accept.status_code == 200

    reference = await client.get("/invitations/this-token-was-never-issued-00000000000000000000")
    preview = await client.get(f"/invitations/{raw}")
    assert preview.status_code == reference.status_code == 404
    assert preview.json()["detail"] == reference.json()["detail"]


# --- POST /invitations/{token}/accept -------------------------------------------


async def test_accepting_joins_the_org_with_the_invited_role(client):
    suffix = "acc1"
    org, owner_id = await _org_with_owner(suffix)
    invitee_email = f"ia-invitee-{suffix}@voiceagent-test.com"
    invite, raw = await inv.create_invitation(org, invitee_email, "admin", owner_id)
    invitee_token = await make_user(invitee_email)
    invitee_id = await _uid(invitee_email)

    r = await client.post(f"/invitations/{raw}/accept", headers=_bearer(invitee_token))
    assert r.status_code == 200
    assert r.json() == {"org_id": org, "role": "admin"}
    membership = await Membership.find_one(Membership.org_id == org, Membership.user_id == invitee_id)
    assert membership is not None
    assert membership.role == "admin"


async def test_a_different_signed_in_user_cannot_accept(client):
    """The link is a bearer credential; matching the address limits a forward."""
    suffix = "mism1"
    org, owner_id = await _org_with_owner(suffix)
    invite, raw = await inv.create_invitation(
        org, f"ia-intended-{suffix}@voiceagent-test.com", "member", owner_id
    )
    someone_else_token = await make_user(f"ia-someone-else-{suffix}@voiceagent-test.com")

    r = await client.post(f"/invitations/{raw}/accept", headers=_bearer(someone_else_token))
    assert r.status_code == 403
    someone_else_id = await _uid(f"ia-someone-else-{suffix}@voiceagent-test.com")
    assert await Membership.find_one(Membership.org_id == org, Membership.user_id == someone_else_id) is None


async def test_accepting_twice_does_not_create_two_memberships(client):
    suffix = "twice1"
    org, owner_id = await _org_with_owner(suffix)
    invitee_email = f"ia-invitee-{suffix}@voiceagent-test.com"
    invite, raw = await inv.create_invitation(org, invitee_email, "member", owner_id)
    invitee_token = await make_user(invitee_email)
    invitee_id = await _uid(invitee_email)

    first = await client.post(f"/invitations/{raw}/accept", headers=_bearer(invitee_token))
    assert first.status_code == 200
    r = await client.post(f"/invitations/{raw}/accept", headers=_bearer(invitee_token))
    assert r.status_code in (409, 200)
    assert await Membership.find(Membership.org_id == org, Membership.user_id == invitee_id).count() == 1


async def test_accepting_without_logging_in_is_401(client):
    suffix = "noauth1"
    org, owner_id = await _org_with_owner(suffix)
    invite, raw = await inv.create_invitation(
        org, f"ia-noauth-{suffix}@voiceagent-test.com", "member", owner_id
    )
    r = await client.post(f"/invitations/{raw}/accept")
    assert r.status_code == 401


async def test_accepting_an_unknown_token_is_404(client):
    r = await client.post(
        "/invitations/this-token-was-never-issued-00000000000000000000/accept",
        headers=_bearer(await make_user("ia-anybody-unknown@voiceagent-test.com")),
    )
    assert r.status_code == 404


async def test_accepting_a_revoked_token_is_404_not_membership(client):
    suffix = "rev1"
    org, owner_id = await _org_with_owner(suffix)
    invitee_email = f"ia-revoked-accept-{suffix}@voiceagent-test.com"
    invite, raw = await inv.create_invitation(org, invitee_email, "member", owner_id)
    assert await inv.revoke(org, str(invite.id))
    invitee_token = await make_user(invitee_email)

    r = await client.post(f"/invitations/{raw}/accept", headers=_bearer(invitee_token))
    assert r.status_code == 404
    invitee_id = await _uid(invitee_email)
    assert await Membership.find_one(Membership.org_id == org, Membership.user_id == invitee_id) is None


async def test_accepting_when_already_a_member_by_other_means_is_409(client):
    """The interrupted-write race accept() documents: find_valid still finds
    the invitation "pending" (accept() never reached the final save last
    time), but add_member finds the membership already there."""
    suffix = "already1"
    org, owner_id = await _org_with_owner(suffix)
    invitee_email = f"ia-already-{suffix}@voiceagent-test.com"
    invite, raw = await inv.create_invitation(org, invitee_email, "member", owner_id)
    invitee_token = await make_user(invitee_email)
    invitee_id = await _uid(invitee_email)
    await Membership(org_id=org, user_id=invitee_id, role="member").insert()

    r = await client.post(f"/invitations/{raw}/accept", headers=_bearer(invitee_token))
    assert r.status_code == 409
    assert await Membership.find(Membership.org_id == org, Membership.user_id == invitee_id).count() == 1


async def test_accepting_an_expired_token_is_404(client):
    suffix = "exp1"
    org, owner_id = await _org_with_owner(suffix)
    invitee_email = f"ia-expired-accept-{suffix}@voiceagent-test.com"
    invite, raw = await inv.create_invitation(org, invitee_email, "member", owner_id)
    invite.expires_at = datetime.now(UTC) - timedelta(days=1)
    await invite.save()
    invitee_token = await make_user(invitee_email)

    r = await client.post(f"/invitations/{raw}/accept", headers=_bearer(invitee_token))
    assert r.status_code == 404


# --- review round 1 ------------------------------------------------------------

async def test_accepting_an_invitation_to_a_deleted_org_creates_no_membership(client):
    """Found in review: delete_org removed the org and its memberships but
    left pending invitations behind, and accept() never checked the org was
    still there. Accepting answered 200 "you joined" and wrote a Membership
    pointing at an organisation that no longer exists."""
    from app.models.organisation import Organisation

    org, inviter = await _org_with_owner("del-1")
    email = "ia-invitee-del-1@voiceagent-test.com"
    invite, raw = await inv.create_invitation(org, email, "member", inviter)
    invitee = await make_user(email)

    await Membership.find(Membership.org_id == org).delete()
    await Organisation.get_motor_collection().delete_one({"_id": PydanticObjectId(org)})

    r = await client.post(f"/invitations/{raw}/accept", headers=_bearer(invitee))
    assert r.status_code == 404
    assert r.json()["detail"] == "Invitation not found"
    assert await Membership.find(Membership.org_id == org).count() == 0


async def test_deleting_an_org_revokes_its_pending_invitations(client):
    """The root cause: an invitation must not outlive the organisation it
    points at, so the link stops working the moment the org is deleted."""
    owner_email = "ia-owner-del-2@voiceagent-test.com"
    owner = await make_user(owner_email)
    personal = _org_of_token[owner]
    made = await client.post("/orgs", json={"name": "Doomed"}, headers=_bearer(owner))
    doomed = made.json()["id"]

    inviter = await _uid(owner_email)
    invite, raw = await inv.create_invitation(doomed, "ia-x-del-2@voiceagent-test.com", "member", inviter)
    assert len(await inv.list_pending(doomed)) == 1

    gone = await client.delete(f"/orgs/{doomed}", headers={**_bearer(owner), "X-Org-Id": doomed})
    assert gone.status_code == 204, gone.text
    assert await inv.list_pending(doomed) == []
    assert personal  # the owner still has their personal org


async def test_a_stranger_cannot_replay_someone_elses_accepted_token(client):
    """The idempotency fallback returns 200 only to the user who actually
    redeemed the token. Loosening that to match on the token alone would
    let any logged-in user replay a used link and learn the org and role."""
    org, inviter = await _org_with_owner("replay-1")
    email = "ia-invitee-replay-1@voiceagent-test.com"
    invite, raw = await inv.create_invitation(org, email, "member", inviter)
    invitee = await make_user(email)
    first = await client.post(f"/invitations/{raw}/accept", headers=_bearer(invitee))
    assert first.status_code == 200

    stranger = await make_user("ia-stranger-replay-1@voiceagent-test.com")
    replay = await client.post(f"/invitations/{raw}/accept", headers=_bearer(stranger))
    nonsense = await client.post("/invitations/not-a-real-token-at-all/accept", headers=_bearer(stranger))
    assert replay.status_code == nonsense.status_code == 404
    assert replay.json() == nonsense.json()


# --- whole-branch review ------------------------------------------------------

async def test_an_expired_invitation_leaves_the_pending_list(client):
    """list_pending filtered on status only, so an expired invitation sat in
    the admin's "Pending invitations" list for ever, with a Revoke button and
    a date in the past. find_valid already excluded it; the list did not."""
    from datetime import UTC, datetime, timedelta

    from app.models.invitation import Invitation

    org, inviter = await _org_with_owner("exp-1")
    invite, _ = await inv.create_invitation(org, "ia-x-exp-1@voiceagent-test.com", "member", inviter)
    assert len(await inv.list_pending(org)) == 1

    invite.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await invite.save()

    assert await inv.list_pending(org) == []
    assert await Invitation.get(invite.id) is not None  # still there, just not listed


async def test_removing_a_member_revokes_the_invitations_they_sent(client):
    """An invitation is an exercise of authority. If the person who sent it
    loses that authority, their pending invitations must not keep handing
    out the role they chose -- for up to 7 days, to whoever holds the link."""
    owner_email = "ia-owner-rights-1@voiceagent-test.com"
    owner = await make_user(owner_email)
    org = _org_of_token[owner]
    admin_email = "ia-admin-rights-1@voiceagent-test.com"
    await make_user(admin_email)
    admin_id = await _uid(admin_email)
    await org_service.add_member(org, admin_id, "admin")

    await inv.create_invitation(org, "ia-guest-rights-1@voiceagent-test.com", "admin", admin_id)
    assert len(await inv.list_pending(org)) == 1

    gone = await client.delete(
        f"/orgs/{org}/members/{admin_id}", headers={**_bearer(owner), "X-Org-Id": org}
    )
    assert gone.status_code == 204, gone.text
    assert await inv.list_pending(org) == []


async def test_demoting_someone_below_admin_revokes_the_invitations_they_sent(client):
    owner_email = "ia-owner-rights-2@voiceagent-test.com"
    owner = await make_user(owner_email)
    org = _org_of_token[owner]
    admin_email = "ia-admin-rights-2@voiceagent-test.com"
    await make_user(admin_email)
    admin_id = await _uid(admin_email)
    await org_service.add_member(org, admin_id, "admin")

    await inv.create_invitation(org, "ia-guest-rights-2@voiceagent-test.com", "admin", admin_id)
    assert len(await inv.list_pending(org)) == 1

    demoted = await client.patch(
        f"/orgs/{org}/members/{admin_id}",
        json={"role": "viewer"},
        headers={**_bearer(owner), "X-Org-Id": org},
    )
    assert demoted.status_code == 200, demoted.text
    assert await inv.list_pending(org) == []


async def test_promoting_someone_leaves_their_invitations_alone(client):
    """Only a LOSS of authority revokes. A promotion must not."""
    owner_email = "ia-owner-rights-3@voiceagent-test.com"
    owner = await make_user(owner_email)
    org = _org_of_token[owner]
    admin_email = "ia-admin-rights-3@voiceagent-test.com"
    await make_user(admin_email)
    admin_id = await _uid(admin_email)
    await org_service.add_member(org, admin_id, "admin")

    await inv.create_invitation(org, "ia-guest-rights-3@voiceagent-test.com", "member", admin_id)

    promoted = await client.patch(
        f"/orgs/{org}/members/{admin_id}",
        json={"role": "owner"},
        headers={**_bearer(owner), "X-Org-Id": org},
    )
    assert promoted.status_code == 200, promoted.text
    assert len(await inv.list_pending(org)) == 1


async def test_demoting_an_owner_to_admin_revokes_only_their_owner_invitations(client):
    """Only an owner may invite someone AS an owner (_guard_owner_rules). An
    owner demoted to admin loses that right but keeps the right to invite
    below owner -- so their owner invitations must go, and the rest stay."""
    owner_email = "ia-owner-rights-4@voiceagent-test.com"
    owner = await make_user(owner_email)
    org = _org_of_token[owner]
    co_owner_email = "ia-coowner-rights-4@voiceagent-test.com"
    await make_user(co_owner_email)
    co_owner_id = await _uid(co_owner_email)
    await org_service.add_member(org, co_owner_id, "owner")

    as_owner, _ = await inv.create_invitation(org, "ia-guest-rights-4a@voiceagent-test.com", "owner", co_owner_id)
    as_admin, _ = await inv.create_invitation(org, "ia-guest-rights-4b@voiceagent-test.com", "admin", co_owner_id)

    demoted = await client.patch(
        f"/orgs/{org}/members/{co_owner_id}",
        json={"role": "admin"},
        headers={**_bearer(owner), "X-Org-Id": org},
    )
    assert demoted.status_code == 200, demoted.text
    assert [i.id for i in await inv.list_pending(org)] == [as_admin.id]


async def test_losing_authority_in_one_org_leaves_invitations_in_another_alone(client):
    """Revocation is per organisation: being removed from one organisation
    says nothing about the authority the same person holds in another."""
    owner_email = "ia-owner-rights-5@voiceagent-test.com"
    owner = await make_user(owner_email)
    org = _org_of_token[owner]
    admin_email = "ia-admin-rights-5@voiceagent-test.com"
    admin = await make_user(admin_email)
    admin_id = await _uid(admin_email)
    their_own_org = _org_of_token[admin]
    await org_service.add_member(org, admin_id, "admin")

    await inv.create_invitation(org, "ia-guest-rights-5a@voiceagent-test.com", "member", admin_id)
    await inv.create_invitation(their_own_org, "ia-guest-rights-5b@voiceagent-test.com", "member", admin_id)

    gone = await client.delete(
        f"/orgs/{org}/members/{admin_id}", headers={**_bearer(owner), "X-Org-Id": org}
    )
    assert gone.status_code == 204, gone.text
    assert await inv.list_pending(org) == []
    assert len(await inv.list_pending(their_own_org)) == 1
