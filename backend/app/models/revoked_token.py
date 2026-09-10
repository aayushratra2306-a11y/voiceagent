from datetime import datetime
from typing import Annotated

from beanie import Document, Indexed


class RevokedRefreshToken(Document):
    """Task 2.5 — the revocation list a Redis set would normally hold. Uses
    MongoDB instead (already the project's one piece of real infrastructure,
    no new service to run for a project still on a zero-budget dev setup) —
    functionally identical for this purpose: an existence check by jti.

    expires_at carries a MongoDB TTL index (expireAfterSeconds=0 means
    "expire exactly at the stored datetime") — a revoked token's record is
    deleted automatically once the token itself would have expired anyway,
    so this collection never grows without bound.

    Review findings I2 and I3 (2026-09-10) — jti is uniquely indexed, and
    that one index does two jobs.

    I2, correctness: recording a revocation is how a refresh token is
    rotated, and rotation is the whole theft-detection story — the thief's
    copy and the owner's copy must not both keep working. Reading "is this
    revoked?" and then writing the revocation is two operations, so two
    refreshes presenting the same cookie together both passed the read
    before either did the write, and both were issued fresh, fully valid
    lineages. Unique means the second insert cannot land, which turns
    "revoke it" into an atomic claim: exactly one caller ever wins, and
    the loser knows it lost.

    I3, speed: the existence check runs on every single refresh, and jti
    was unindexed — a collection scan on a hot, authenticated path, over a
    collection that grows with every logout and every rotation.
    """

    jti: Annotated[str, Indexed(unique=True)]
    expires_at: Indexed(datetime, expireAfterSeconds=0)

    class Settings:
        name = "revoked_refresh_tokens"
