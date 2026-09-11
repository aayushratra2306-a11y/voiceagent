"""Review finding I6 (2026-09-10) — two problems at opposite ends of the
same field.

At the short end there was no validator at all: `RegisterRequest.password`
was a bare `str`, so "" and "a" were acceptable passwords for a system
holding payment-adjacent customer data.

At the long end, bcrypt silently ignores everything past the 72nd byte.
Not an error — it truncates and carries on. So two passwords sharing a
72-byte prefix were the same password, and either logged you in as the
other. The people most exposed were the ones doing the right thing, since
a long random password is what a password manager produces.

The second is the one that needed care to fix, because changing a hashing
scheme can lock out every existing user at once. `bcrypt` stays in the
context behind `bcrypt_sha256`, so old hashes still verify; login rewrites
them as it goes. Both halves are asserted here.
"""

import pytest

from app.core.security import hash_password, needs_rehash, verify_password

pytestmark = pytest.mark.asyncio(loop_scope="session")


# A 72-byte prefix is the truncation boundary; what follows it is what
# plain bcrypt could not see.
SHARED_PREFIX = "A" * 72
REAL = SHARED_PREFIX + "-the-actual-secret"
IMPOSTOR = SHARED_PREFIX + "-something-else-entirely"


# ---------------------------------------------------------------- hashing


def test_a_long_password_is_not_truncated_to_its_first_72_bytes():
    """The bug, stated directly. Under plain bcrypt this assertion failed:
    IMPOSTOR verified against REAL's hash, because bcrypt never saw the
    bytes that distinguish them."""
    stored = hash_password(REAL)

    assert verify_password(REAL, stored)
    assert not verify_password(IMPOSTOR, stored)


def test_an_existing_bcrypt_hash_still_verifies():
    """The one that decides whether this change is shippable.

    Every password in the database was hashed with plain bcrypt. If adding
    bcrypt_sha256 stopped those verifying, the deploy would lock out every
    user at once — so the legacy scheme stays in the context, and this is
    the assertion that says so.
    """
    from passlib.context import CryptContext

    legacy_bcrypt_only = CryptContext(schemes=["bcrypt"], deprecated="auto")
    stored_before_the_change = legacy_bcrypt_only.hash("a-password-from-before")

    assert verify_password("a-password-from-before", stored_before_the_change)
    assert not verify_password("the-wrong-one", stored_before_the_change)


def test_a_legacy_hash_is_flagged_for_upgrade_and_a_new_one_is_not():
    """What drives the rewrite-on-login below. A legacy hash is not broken,
    it is just still carrying the truncation weakness, so it is marked
    rather than rejected."""
    from passlib.context import CryptContext

    legacy = CryptContext(schemes=["bcrypt"], deprecated="auto").hash("x" * 20)

    assert needs_rehash(legacy)
    assert not needs_rehash(hash_password("x" * 20))


# ------------------------------------------------------- the length floor


async def test_a_short_password_is_refused_at_registration(client):
    res = await client.post(
        "/auth/register",
        json={"email": "shorty@example.com", "password": "short"},
    )

    assert res.status_code == 422


async def test_an_empty_password_is_refused_at_registration(client):
    res = await client.post(
        "/auth/register",
        json={"email": "empty@example.com", "password": ""},
    )

    assert res.status_code == 422


async def test_a_long_enough_password_is_accepted(client):
    """The floor must not be so eager that an ordinary sign-up fails."""
    res = await client.post(
        "/auth/register",
        json={"email": "ordinary@example.com", "password": "correct horse battery staple"},
    )

    assert res.status_code == 201


async def test_a_very_long_password_is_accepted_and_usable(client):
    """There is deliberately no upper bound. Accepting one was only safe
    once truncation was fixed — before that, the longer the password, the
    more of it was thrown away."""
    long_password = "x" * 200

    created = await client.post(
        "/auth/register",
        json={"email": "verbose@example.com", "password": long_password},
    )
    assert created.status_code == 201

    signed_in = await client.post(
        "/auth/login",
        json={"email": "verbose@example.com", "password": long_password},
    )
    assert signed_in.status_code == 200


# --------------------------------------------------- upgrade on next login


async def test_logging_in_rewrites_a_legacy_hash_in_place(client):
    """Login is the only moment the plaintext exists, so it is the only
    place an old hash can be upgraded. After one successful sign-in the
    stored hash must be the new scheme — and the account must still work.
    """
    from passlib.context import CryptContext

    from app.models.user import User

    password = "a-perfectly-good-password"
    legacy_hash = CryptContext(schemes=["bcrypt"], deprecated="auto").hash(password)
    user = User(email="legacy@example.com", password_hash=legacy_hash)
    await user.insert()

    assert needs_rehash(user.password_hash), "precondition: stored as the old scheme"

    res = await client.post(
        "/auth/login", json={"email": "legacy@example.com", "password": password},
    )
    assert res.status_code == 200

    refreshed = await User.find_one(User.email == "legacy@example.com")
    assert not needs_rehash(refreshed.password_hash), "hash was not upgraded"
    assert verify_password(password, refreshed.password_hash), "upgrade broke the password"


async def test_a_failed_login_does_not_rewrite_anything(client):
    """The rewrite hangs off a *successful* verify. A wrong password must
    not touch the stored hash."""
    from passlib.context import CryptContext

    from app.models.user import User

    legacy_hash = CryptContext(schemes=["bcrypt"], deprecated="auto").hash("the-right-one")
    await User(email="untouched@example.com", password_hash=legacy_hash).insert()

    res = await client.post(
        "/auth/login", json={"email": "untouched@example.com", "password": "the-wrong-one"},
    )
    assert res.status_code == 401

    refreshed = await User.find_one(User.email == "untouched@example.com")
    assert refreshed.password_hash == legacy_hash
