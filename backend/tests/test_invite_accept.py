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

from app.models.organisation import Membership
from app.models.user import User
from app.services import invitations as inv
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
