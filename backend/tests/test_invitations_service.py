from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.models.invitation import Invitation
from app.services import invitations as inv

pytestmark = pytest.mark.asyncio(loop_scope="session")


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
