import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.core.config import settings
from app.models.revoked_token import RevokedRefreshToken
from app.models.user import User

bearer_scheme = HTTPBearer()

REFRESH_COOKIE_NAME = "refresh_token"


def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["type"] = "access"
    now = datetime.now(UTC)
    # iat (standard JWT claim, RFC 7519): without it, two tokens minted for
    # the same user in the same second — e.g. login immediately followed by
    # a refresh — encode to the exact same bytes (same header+payload+key
    # -> deterministic signature), which is harmless but made that sequence
    # look like a no-op when testing it live. iat also just makes the token
    # more inspectable/auditable regardless.
    payload["iat"] = now
    payload["exp"] = now + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def create_refresh_token(data: dict) -> tuple[str, str, datetime]:
    """Task 2.5 — a long-lived companion to the short access token, stored
    only in an httpOnly cookie (never touched by frontend JS, so an XSS bug
    can't exfiltrate it the way it could steal a localStorage token).

    Returns (token, jti, expires_at) — jti/expires_at are what
    revoke_refresh_token needs to record a revocation without re-decoding
    the token later.
    """
    jti = str(uuid.uuid4())
    expire = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)
    payload = {**data, "type": "refresh", "jti": jti, "exp": expire}
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)
    return token, jti, expire


async def verify_refresh_token(token: str) -> tuple[str, str, datetime]:
    """Returns (email, jti, expires_at) if the refresh token is valid,
    correctly typed, and not revoked. Raises HTTPException(401) otherwise —
    every failure mode (expired, tampered, wrong type, revoked) gets the
    same response, deliberately: telling a caller *which* check failed is
    free information for an attacker probing a stolen/guessed token."""
    invalid = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token")
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError as e:
        raise invalid from e

    if payload.get("type") != "refresh":
        raise invalid
    email, jti, exp = payload.get("sub"), payload.get("jti"), payload.get("exp")
    if not email or not jti or not exp:
        raise invalid

    if await RevokedRefreshToken.find_one(RevokedRefreshToken.jti == jti):
        raise invalid

    return email, jti, datetime.fromtimestamp(exp, tz=UTC)


async def revoke_refresh_token(token: str) -> None:
    """Task 2.5 — this is what makes logout actually mean something server-
    side, not just "the browser forgot the token": the token's jti is
    recorded so verify_refresh_token rejects it even if someone captured a
    copy of it before logout. A token that's already invalid/expired/
    malformed has nothing to revoke — silently no-ops rather than raising,
    since logout should never itself fail."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        return
    jti, exp = payload.get("jti"), payload.get("exp")
    if not jti or not exp:
        return
    await RevokedRefreshToken(jti=jti, expires_at=datetime.fromtimestamp(exp, tz=UTC)).insert()


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(credentials.credentials, settings.secret_key, algorithms=[settings.algorithm])
        email: str = payload.get("sub")
        # Task 2.5: reject a refresh token presented as an access token —
        # they're both just JWTs signed with the same key, so without this
        # check a stolen/leaked refresh token would work as a (long-lived!)
        # access token too, defeating the whole point of separating them.
        if email is None or payload.get("type") != "access":
            raise credentials_exception
    except JWTError as e:
        raise credentials_exception from e

    user = await User.find_one(User.email == email)
    if user is None:
        raise credentials_exception
    return user
