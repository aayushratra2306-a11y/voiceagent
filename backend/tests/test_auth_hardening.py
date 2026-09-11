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


# =========================================================================
# I2 / I3 — refresh-token rotation was not atomic, and the revocation
#           list was unindexed
# =========================================================================


async def test_the_revocation_list_is_uniquely_indexed_by_jti():
    """I3 as well as I2. Only `expires_at` carried an index (the TTL one),
    so the existence check every single refresh performs — "has this jti
    been revoked?" — was a collection scan on the hot path. The unique
    index I2 needs to make the claim atomic is the same index I3 needs to
    make the lookup fast, so one change answers both.
    """
    from app.models.revoked_token import RevokedRefreshToken

    indexes = await RevokedRefreshToken.get_motor_collection().index_information()
    unique_on_jti = [
        name
        for name, spec in indexes.items()
        if spec.get("key") == [("jti", 1)] and spec.get("unique")
    ]
    assert unique_on_jti, f"no unique index on jti; have {list(indexes)}"
    # The TTL index is what stops this collection growing forever — it must
    # survive the change, not be replaced by it.
    ttl = [
        name for name, spec in indexes.items()
        if spec.get("key") == [("expires_at", 1)] and spec.get("expireAfterSeconds") == 0
    ]
    assert ttl, f"the TTL index on expires_at is gone; have {list(indexes)}"


async def _logged_in_client():
    """A client holding a live refresh cookie, and that cookie's value."""
    email = f"rotate-{next(_next_fake_ip)}@voiceagent-test.com"
    client = await _fresh_client()
    await client.post("/auth/register", json={"email": email, "password": PASSWORD})
    await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return client, client.cookies["refresh_token"]


async def test_two_refreshes_with_one_cookie_leave_exactly_one_live_lineage(monkeypatch):
    """Rotation is what makes a stolen refresh token detectable: the
    thief's copy and the owner's copy cannot both keep working, because
    whichever is used next invalidates the other.

    Verifying and then revoking is two steps, and there was no unique
    index on jti to stop the second writer, so two refreshes presenting
    the same cookie at the same time both got past `verify_refresh_token`
    before either recorded a revocation. Both were handed a brand new,
    fully valid refresh token, and the theft-detection guarantee quietly
    stopped holding: two lineages ran on side by side, neither ever
    stepping on the other.

    The window between those two statements is a few hundred microseconds
    wide, so simply firing two requests at once hits it rarely enough that
    the test would pass against the broken code most runs — which is worse
    than no test. Both requests are therefore parked in the window on
    purpose: `verify_refresh_token` is wrapped so that each request waits
    there until the other has also finished verifying. That is not a
    contrived scenario, it is the same scenario with the timing made
    reliable — a page firing several API calls as the access token expires
    produces it for real.
    """
    from app.api import auth as auth_api

    client, cookie = await _logged_in_client()
    real_verify = auth_api.verify_refresh_token
    both_verified = asyncio.Event()
    arrived = 0

    async def gated_verify(token: str):
        nonlocal arrived
        result = await real_verify(token)
        arrived += 1
        if arrived >= 2:
            both_verified.set()
        try:
            await asyncio.wait_for(both_verified.wait(), timeout=10)
        except TimeoutError:
            pass  # the other request never got here; let this one proceed
        return result

    monkeypatch.setattr(auth_api, "verify_refresh_token", gated_verify)

    try:
        async def attempt():
            # Separate clients so httpx's own cookie jar cannot serialise
            # them or rewrite the cookie out from under the other one.
            async with await _fresh_client() as c:
                c.cookies.set("refresh_token", cookie)
                resp = await c.post("/auth/refresh")
                return resp.status_code

        statuses = await asyncio.gather(*(attempt() for _ in range(2)))
    finally:
        await client.aclose()

    assert arrived == 2, "the two requests never overlapped; the test proves nothing"
    assert statuses.count(200) == 1, f"more than one lineage survived: {statuses}"
    assert statuses.count(401) == 1, f"expected the loser to be refused as reuse: {statuses}"


async def test_the_ordinary_rotation_still_works():
    """The sequential path is the one every real session takes — it must
    still hand back a working token, not get caught by the new claim."""
    client, _cookie = await _logged_in_client()
    try:
        resp = await client.post("/auth/refresh")
        assert resp.status_code == 200
        token = resp.json()["access_token"]
        me = await client.get("/bots/", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
    finally:
        await client.aclose()
