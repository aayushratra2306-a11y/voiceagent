"""Password hashing.

Review finding I6 (2026-09-10) changed the scheme, and the reason is worth
keeping because it is not the obvious one.

bcrypt ignores everything past the 72nd byte of its input. Not an error —
it silently truncates. So with plain bcrypt, `("A"*72) + "REAL-SECRET"` and
`("A"*72) + "SOMETHING-ELSE"` are the same password, and either one logs
you in as the other. Verified directly against the installed passlib
before this change: the cross-check returned True.

Long passwords are exactly what a careful user or a password manager
produces, so the people most likely to hit this are the ones who did the
right thing.

`bcrypt_sha256` fixes it by SHA-256'ing the password first and feeding the
digest to bcrypt, which is always well under the limit. `bcrypt` stays in
the list, second, so every hash already in the database still verifies —
also confirmed directly, because getting this wrong locks out every
existing user at once. `deprecated="auto"` marks those legacy hashes as
needing an upgrade, and login rehashes them in place on the next
successful sign-in (see api/auth.py), so the old scheme drains away on its
own rather than needing a migration.
"""

from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def needs_rehash(hashed_password: str) -> bool:
    """Whether this hash uses a scheme we no longer issue.

    True for every bcrypt hash written before the I6 change. The caller has
    the plaintext only for the instant it is being verified, so this is
    asked at login and nowhere else — see api/auth.py.
    """
    return pwd_context.needs_update(hashed_password)
