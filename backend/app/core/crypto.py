"""Encryption for secrets this system stores on a customer's behalf.

Task 3.1 lets a customer configure a tool that calls their own API, which
means storing their API key. That is a different kind of secret from a
password: a password is only ever compared, so it is hashed and never
recoverable, whereas this key has to be sent to their server on every call
and therefore has to be recoverable. Hashing it would make it useless.

So it is encrypted at rest with Fernet (AES-128-CBC with an HMAC, from the
`cryptography` package) rather than hashed. A database dump alone does not
yield the keys; an attacker needs SECRET_KEY as well.

The honest limits, worth stating rather than implying otherwise:

  - The key is derived from SECRET_KEY, so anyone who can read the running
    process's environment can decrypt. That is the same trust boundary the
    JWT signing key already sits behind, and raising it means a managed KMS
    (AWS KMS, GCP KMS) — appropriate later, over-engineered for one VM.
  - Rotating SECRET_KEY makes every stored credential undecryptable. There
    is no re-encryption path yet; adding one means storing a key id
    alongside each value.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


def _fernet() -> Fernet:
    """Build the cipher from SECRET_KEY.

    SHA-256 rather than the raw key because Fernet needs exactly 32 bytes
    and SECRET_KEY is an arbitrary-length string. Not a password-stretching
    KDF on purpose: SECRET_KEY is already high-entropy machine-generated,
    and this runs on every tool call, where a deliberately slow derivation
    would be latency spent for no security gain.
    """
    digest = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a credential for storage. Returns text safe to put in Mongo."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Recover a stored credential.

    Returns "" for anything that will not decrypt rather than raising. A
    tool whose credential is unreadable — SECRET_KEY rotated, the row
    hand-edited — should fail as an unauthorised call to the customer's own
    API, which is legible, instead of a 500 from somewhere unrelated.
    """
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


def mask_secret(plaintext: str) -> str:
    """A form safe to show in the UI: last four characters only.

    Enough for someone to recognise which key they configured, useless to
    anyone who obtains it.
    """
    if not plaintext:
        return ""
    if len(plaintext) <= 4:
        return "•" * len(plaintext)
    return "•" * 8 + plaintext[-4:]
