import asyncio

import pytest

from app.models.organisation import Membership, Organisation
from app.models.user import User
from app.services.orgs import ensure_personal_org

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _user(email: str) -> User:
    user = User(email=email, password_hash="x")
    await user.insert()
    return user


async def test_a_user_without_an_organisation_gets_a_personal_one_they_own():
    user = await _user("personal-1@voiceagent-test.com")

    org = await ensure_personal_org(user)

    assert org.personal is True and org.owner_count == 1
    m = await Membership.find_one(Membership.org_id == str(org.id), Membership.user_id == str(user.id))
    assert m.role == "owner"


async def test_a_second_call_changes_nothing():
    user = await _user("personal-2@voiceagent-test.com")
    await ensure_personal_org(user)

    assert await ensure_personal_org(user) is None
    assert await Organisation.find(Organisation.created_by == str(user.id)).count() == 1


async def test_concurrent_calls_leave_exactly_one_personal_org_and_one_membership():
    user = await _user("personal-3@voiceagent-test.com")

    await asyncio.gather(*(ensure_personal_org(user) for _ in range(5)))

    orgs = await Organisation.find(Organisation.created_by == str(user.id), Organisation.personal == True).to_list()  # noqa: E712
    assert len(orgs) == 1
    assert await Membership.find(Membership.user_id == str(user.id)).count() == 1
    assert orgs[0].owner_count == 1


async def test_someone_removed_from_their_personal_org_gets_a_fresh_one():
    """Their old personal org has other members now; it must not be reused."""
    user = await _user("personal-4@voiceagent-test.com")
    old = await ensure_personal_org(user)
    other = await _user("personal-4-other@voiceagent-test.com")
    await Membership(org_id=str(old.id), user_id=str(other.id), role="owner").insert()
    await Membership.find(Membership.user_id == str(user.id)).delete()

    new = await ensure_personal_org(user)

    assert new.id != old.id
    assert (await Organisation.get(old.id)).personal is False


async def test_registering_creates_a_personal_org(client):
    await client.post("/auth/register", json={"email": "personal-5@voiceagent-test.com", "password": "testpass12345"})
    user = await User.find_one(User.email == "personal-5@voiceagent-test.com")
    assert await Membership.find(Membership.user_id == str(user.id)).count() == 1
