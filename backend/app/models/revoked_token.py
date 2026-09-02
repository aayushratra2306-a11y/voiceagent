from datetime import datetime

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
    """

    jti: str
    expires_at: Indexed(datetime, expireAfterSeconds=0)

    class Settings:
        name = "revoked_refresh_tokens"
