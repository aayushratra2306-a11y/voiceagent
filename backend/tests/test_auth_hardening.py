# Review findings I1, I2, I3, I6 (2026-09-10) — the account and session
# layer, hardened.
#
# Every test in here is about a race or a truncation, which is exactly the
# class of bug that passes a manual click-through and a sequential test
# suite and then happens in production anyway. They are written against the
# real database (see conftest.py) rather than a mock, because what is being
# asserted IS the database's behaviour: a unique index either exists and
# rejects the second writer, or it does not.
import asyncio
import itertools

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Same reasoning as test_auth.py: the rate-limit key falls back to per-IP
# for unauthenticated requests and ASGITransport hands every client the
# same fake address, so without this every test in this file would share
# one 5/minute bucket with every other one.
_next_fake_ip = itertools.count(100)


async def _fresh_client() -> AsyncClient:
    from main import app

    fake_ip = f"10.1.0.{next(_next_fake_ip)}"
    transport = ASGITransport(app=app, client=(fake_ip, 12345))
    return AsyncClient(transport=transport, base_url="https://test")


# A password long enough for the I6 floor, used everywhere below so these
# tests are about what they say they are about and not about length.
PASSWORD = "correct-horse-battery-staple"


# =========================================================================
# I1 — two simultaneous signups for one address both persisted
# =========================================================================


async def test_email_carries_a_unique_index():
    """The guarantee has to live in the database, not in a Python `if`.

    Asserted directly on the collection's index list rather than inferred
    from behaviour: an index that exists is the only thing that makes the
    concurrent test below deterministic rather than lucky.
    """
    from app.models.user import User

    indexes = await User.get_motor_collection().index_information()
    unique_on_email = [
        name
        for name, spec in indexes.items()
        if spec.get("key") == [("email", 1)] and spec.get("unique")
    ]
    assert unique_on_email, f"no unique index on email; have {list(indexes)}"


async def test_two_simultaneous_signups_for_one_address_leave_one_account():
    """Register used to read-then-write: `find_one` for an existing user,
    then `insert`. Two requests arriving together both complete the read
    before either does the write, so both saw "nobody has this address"
    and both inserted. Login's own `find_one` then returns whichever of the
    two Mongo happens to hand back — possibly the other person's password
    hash against this person's address.
    """
    from app.models.user import User

    email = "race-signup@voiceagent-test.com"

    async def signup():
        async with await _fresh_client() as client:
            resp = await client.post(
                "/auth/register", json={"email": email, "password": PASSWORD}
            )
            return resp.status_code

    statuses = await asyncio.gather(*(signup() for _ in range(4)))

    stored = await User.find(User.email == email).count()
    assert stored == 1, f"{stored} accounts exist for one address (statuses={statuses})"
    assert statuses.count(201) == 1, f"more than one signup was told it succeeded: {statuses}"
    # Everybody else gets the same answer the old pre-check gave, so the
    # frontend's error handling is unchanged.
    assert set(statuses) - {201} == {400}, statuses


async def test_a_duplicate_signup_still_reads_as_a_plain_rejection():
    """The sequential case, which is the one users actually hit — it must
    not have regressed into a 500 now that the pre-check is gone."""
    async with await _fresh_client() as client:
        first = await client.post(
            "/auth/register",
            json={"email": "dup-signup@voiceagent-test.com", "password": PASSWORD},
        )
        second = await client.post(
            "/auth/register",
            json={"email": "dup-signup@voiceagent-test.com", "password": PASSWORD},
        )

    assert first.status_code == 201
    assert second.status_code == 400
    assert second.json()["detail"] == "Email already registered"
