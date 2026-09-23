from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.models.invitation import Invitation
from app.models.organisation import Membership, Organisation
from app.models.user import User
from app.services import invitations as inv
from app.services import orgs as org_service

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_org() -> Organisation:
    """A real organisation with one owner (an arbitrary user id — Membership
    and Organisation.created_by are plain strings, not a User foreign key,
    same as tests/test_org_members.py's own `_org` helper)."""
    org = Organisation(name="Invite Test Org", created_by="org-owner", owner_count=0)
    await org.insert()
    await org_service.add_member(str(org.id), "org-owner", "owner")
    return org


async def _make_user(email: str) -> User:
    """A real, persisted User — accept() reads user.id and user.email off
    of it, so a stub object would not exercise the real code path."""
    user = User(email=email, password_hash="not-used-in-this-test")
    await user.insert()
    return user


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _clean_invitations():
    # No test in this file gives its org/email its own unique identifier —
    # every function below reuses the literal "org1"/"org2"/"a@b.com" the
    # brief specifies verbatim, and conftest.py only drops the whole
    # database once at the end of the session (see its _test_db fixture).
    # Without this, list_pending's assertion of an exact email list would
    # see every earlier test's still-"pending" org1 invitations too.
    await Invitation.get_motor_collection().delete_many({})
    yield


async def test_the_raw_token_is_never_stored():
    """A database leak must not hand out working invitation links."""
    invite, raw = await inv.create_invitation("org1", "a@b.com", "member", "user1")
    assert raw not in invite.token_hash
    assert invite.token_hash == inv.hash_token(raw)
    stored = await Invitation.get(invite.id)
    assert raw not in stored.model_dump_json()


async def test_the_email_is_normalised():
    invite, _ = await inv.create_invitation("org1", "  Mixed.Case@B.com ", "member", "u1")
    assert invite.email == "mixed.case@b.com"


async def test_an_invitation_expires_after_seven_days():
    invite, _ = await inv.create_invitation("org1", "a@b.com", "member", "u1")
    days = (invite.expires_at - invite.created_at).days
    assert days == inv.INVITATION_EXPIRY_DAYS == 7


async def test_a_valid_token_is_found():
    invite, raw = await inv.create_invitation("org1", "a@b.com", "member", "u1")
    assert (await inv.find_valid(raw)).id == invite.id


async def test_an_unknown_token_is_not_found():
    with pytest.raises(inv.NotFoundError):
        await inv.find_valid("nope")


async def test_an_expired_invitation_is_not_found():
    invite, raw = await inv.create_invitation("org1", "a@b.com", "member", "u1")
    invite.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await invite.save()
    with pytest.raises(inv.NotFoundError):
        await inv.find_valid(raw)


async def test_a_revoked_invitation_is_not_found():
    invite, raw = await inv.create_invitation("org1", "a@b.com", "member", "u1")
    await inv.revoke("org1", str(invite.id))
    with pytest.raises(inv.NotFoundError):
        await inv.find_valid(raw)


async def test_revoke_is_org_scoped():
    """Another organisation's id must never revoke this invitation."""
    invite, raw = await inv.create_invitation("org1", "a@b.com", "member", "u1")
    assert await inv.revoke("org2", str(invite.id)) is False
    assert (await inv.find_valid(raw)).id == invite.id


async def test_list_pending_is_org_scoped_and_excludes_finished_ones():
    a, _ = await inv.create_invitation("org1", "a@b.com", "member", "u1")
    b, _ = await inv.create_invitation("org1", "b@b.com", "member", "u1")
    await inv.create_invitation("org2", "c@b.com", "member", "u1")
    await inv.revoke("org1", str(b.id))
    pending = await inv.list_pending("org1")
    assert [i.email for i in pending] == ["a@b.com"]


# --- accept() -----------------------------------------------------------------


async def test_accept_joins_the_org_with_the_invited_role_and_marks_accepted():
    org = await _make_org()
    user = await _make_user("accept-1@x.com")
    invite, raw = await inv.create_invitation(str(org.id), "accept-1@x.com", "admin", "org-owner")

    accepted = await inv.accept(raw, user)

    assert accepted.status == "accepted"
    assert accepted.accepted_at is not None
    assert accepted.accepted_by == str(user.id)

    membership = await Membership.find_one(
        Membership.org_id == str(org.id), Membership.user_id == str(user.id)
    )
    assert membership is not None
    assert membership.role == "admin"


async def test_accept_with_a_mismatched_email_raises_and_creates_no_membership():
    org = await _make_org()
    user = await _make_user("wrong-person@x.com")
    invite, raw = await inv.create_invitation(str(org.id), "invitee@x.com", "member", "org-owner")

    with pytest.raises(inv.EmailMismatchError):
        await inv.accept(raw, user)

    membership = await Membership.find_one(
        Membership.org_id == str(org.id), Membership.user_id == str(user.id)
    )
    assert membership is None
    # And the invitation itself must be untouched.
    assert (await Invitation.get(invite.id)).status == "pending"


async def test_accept_for_someone_already_a_member_raises_already_member_error():
    org = await _make_org()
    user = await _make_user("accept-2@x.com")
    await org_service.add_member(str(org.id), str(user.id), "member")
    invite, raw = await inv.create_invitation(str(org.id), "accept-2@x.com", "admin", "org-owner")

    with pytest.raises(inv.AlreadyMemberError):
        await inv.accept(raw, user)

    count = await Membership.find(
        Membership.org_id == str(org.id), Membership.user_id == str(user.id)
    ).count()
    assert count == 1


async def test_accept_of_an_expired_invitation_raises_not_found():
    org = await _make_org()
    user = await _make_user("accept-3@x.com")
    invite, raw = await inv.create_invitation(str(org.id), "accept-3@x.com", "member", "org-owner")
    invite.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await invite.save()

    with pytest.raises(inv.NotFoundError):
        await inv.accept(raw, user)

    membership = await Membership.find_one(
        Membership.org_id == str(org.id), Membership.user_id == str(user.id)
    )
    assert membership is None


async def test_accept_of_a_revoked_invitation_raises_not_found():
    org = await _make_org()
    user = await _make_user("accept-4@x.com")
    invite, raw = await inv.create_invitation(str(org.id), "accept-4@x.com", "member", "org-owner")
    await inv.revoke(str(org.id), str(invite.id))

    with pytest.raises(inv.NotFoundError):
        await inv.accept(raw, user)

    membership = await Membership.find_one(
        Membership.org_id == str(org.id), Membership.user_id == str(user.id)
    )
    assert membership is None


async def test_accept_after_the_documented_interruption_raises_already_member_not_a_duplicate():
    """Reproduces the scenario accept()'s own docstring describes: the
    membership write went through but the process died before the
    invitation was saved as accepted, so it is left "pending" for someone
    who is already a member. A retry of accept() must not create a second
    membership — it must surface AlreadyMemberError."""
    org = await _make_org()
    user = await _make_user("accept-5@x.com")
    invite, raw = await inv.create_invitation(str(org.id), "accept-5@x.com", "member", "org-owner")

    # Simulate the interruption directly: do the membership half of accept()
    # by hand, and leave the invitation "pending" (do not call accept()).
    await org_service.add_member(str(org.id), str(user.id), invite.role)
    assert (await Invitation.get(invite.id)).status == "pending"

    with pytest.raises(inv.AlreadyMemberError):
        await inv.accept(raw, user)

    count = await Membership.find(
        Membership.org_id == str(org.id), Membership.user_id == str(user.id)
    ).count()
    assert count == 1


async def test_accept_matches_the_invited_address_regardless_of_stored_case_by_design():
    """Documents deliberate, current behaviour — NOT a bug and NOT to be
    "fixed" by weakening this test. _normalise_email lowercases the whole
    address, including the local part, so this invitation (to the already-
    lowercase "someone@x.com") is accepted by a user whose stored email is
    "Someone@x.com". This is intentional: see _normalise_email's docstring.
    The users index does NOT apply the same rule (EmailStr only lowercases
    the domain), so two accounts differing only in local-part case could
    both exist and both satisfy this one invitation — a pre-existing users-
    index gap, out of scope for this change."""
    org = await _make_org()
    user = await _make_user("Someone@x.com")
    invite, raw = await inv.create_invitation(str(org.id), "someone@x.com", "member", "org-owner")

    accepted = await inv.accept(raw, user)

    assert accepted.status == "accepted"
    membership = await Membership.find_one(
        Membership.org_id == str(org.id), Membership.user_id == str(user.id)
    )
    assert membership is not None


# --- deliver_invitation() -------------------------------------------------


async def test_deliver_invitation_never_logs_the_token_or_the_url():
    """The token is the only thing gating org membership, and logs have
    broader access and longer retention than the database — logging it (or
    a URL built from it) would be worse than storing it. Asserted on the
    actual loguru sink, not pytest's caplog: this project logs through
    loguru, which does not feed caplog, so a caplog assertion would pass
    whether the bug existed or not (see test_phase4_disclosure.py)."""
    from loguru import logger

    org = await _make_org()
    invite, raw = await inv.create_invitation(str(org.id), "log-test@x.com", "member", "org-owner")
    url = f"https://example.com/invite?token={raw}"

    lines: list[str] = []
    sink = logger.add(lines.append, level="INFO")
    try:
        await inv.deliver_invitation(invite, url)
    finally:
        logger.remove(sink)

    logged = "\n".join(lines)
    assert raw not in logged
    assert url not in logged
    assert str(invite.id) in logged
    assert invite.email in logged
    assert invite.org_id in logged
