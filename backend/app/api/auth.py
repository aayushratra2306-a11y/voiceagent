from fastapi import APIRouter, HTTPException, Request, Response, status
from loguru import logger
from pydantic import BaseModel, EmailStr, Field
from pymongo.errors import DuplicateKeyError

from app.core.auth import (
    REFRESH_COOKIE_NAME,
    create_access_token,
    create_refresh_token,
    revoke_refresh_token,
    verify_refresh_token,
)
from app.core.rate_limit import limiter
from app.core.security import hash_password, needs_rehash, verify_password
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    # Review finding I6 (2026-09-10) — there was no validator here at all,
    # so "" and "a" were both acceptable passwords for a system holding
    # payment-adjacent customer data.
    #
    # A length floor and nothing else, deliberately. Composition rules
    # ("one uppercase, one digit, one symbol") mostly push people toward
    # Password1! and are no longer recommended by NIST; length is what
    # actually costs an attacker. 12 is the floor, with no maximum beyond
    # what the hash accepts — see core/security.py on why a long password
    # is now safe to accept, which it genuinely was not before.
    password: str = Field(min_length=12)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


def _set_refresh_cookie(response: Response, token: str, expires_at) -> None:
    # httponly: frontend JS can never read this, so an XSS bug can't
    # exfiltrate it the way it could steal a token sitting in localStorage.
    # secure: browsers exempt "localhost" from requiring an actual HTTPS
    # context for this flag, so it still works in local dev over plain
    # http://localhost — this does need real HTTPS once deployed anywhere
    # else, which is a fair assumption for a cookie carrying a credential.
    # samesite="lax": sent on normal navigation/same-site fetches, withheld
    # on cross-site requests — the standard CSRF-mitigation default.
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/auth",
        expires=expires_at,
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def register(request: Request, body: RegisterRequest):
    # Review finding I1 (2026-09-10) — no pre-check any more.
    #
    # This used to `find_one` for an existing account and then `insert`.
    # Between those two statements is a window, and two signups for the
    # same address arriving inside it both read "nobody has this" and both
    # inserted. The check read like a guard and was not one: it can only
    # see writes that already finished.
    #
    # The unique index on User.email (see models/user.py) is the guard, and
    # it is one because MongoDB applies it to whichever write arrives
    # second no matter how close together they are. So: just insert, and
    # let the duplicate-key error tell us. The response is deliberately the
    # same 400 the pre-check returned, so nothing downstream changes.
    user = User(email=body.email, password_hash=hash_password(body.password))
    try:
        await user.insert()
    except DuplicateKeyError as e:
        raise HTTPException(status_code=400, detail="Email already registered") from e
    return {"message": "User created successfully"}


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
async def login(request: Request, body: LoginRequest, response: Response):
    user = await User.find_one(User.email == body.email)
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Review finding I6 (2026-09-10) — drain the old bcrypt hashes.
    #
    # Every password stored before that change used plain bcrypt, which
    # silently ignores everything past the 72nd byte (see core/security.py).
    # Those hashes still verify, so nobody is locked out, but they keep the
    # weakness until they are rewritten — and this is the only moment the
    # plaintext exists to rewrite them from.
    #
    # Best-effort on purpose: a failed write here must not turn a correct
    # password into a failed login. The user keeps their old hash and the
    # next sign-in tries again.
    if needs_rehash(user.password_hash):
        try:
            user.password_hash = hash_password(body.password)
            await user.save()
        except Exception as e:
            logger.warning(f"[AUTH] Could not upgrade password hash: {type(e).__name__}: {e}")

    access_token = create_access_token({"sub": user.email})
    refresh_token, _jti, expires_at = create_refresh_token({"sub": user.email})
    _set_refresh_cookie(response, refresh_token, expires_at)
    return TokenResponse(access_token=access_token)


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("20/minute")
async def refresh(request: Request, response: Response):
    """Task 2.5 — exchanges a still-valid refresh cookie for a new access
    token. Rotates the refresh token on every use (issue a new one, revoke
    the old one) rather than reusing it: if a refresh token is ever stolen,
    rotation means the thief's copy and the legitimate owner's copy can't
    both keep working — whichever uses it next invalidates the other,
    which is itself a detectable signal, not a silent, indefinite leak."""
    token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="No refresh token")

    email, _jti, _exp = await verify_refresh_token(token)
    # Review finding I2 (2026-09-10) — the revocation IS the claim, and
    # losing it means somebody else is already rotating this exact token.
    #
    # Verification above only proves the token was valid a moment ago. It
    # cannot prove this request is the only one holding it, because the
    # request racing this one passed the same check. `revoke_refresh_token`
    # now settles that atomically (a unique index on jti), and a False
    # answer means this cookie was spent by someone else — which is the
    # definition of refresh-token reuse, so it is refused.
    #
    # Deliberately the same 401 and the same wording verify_refresh_token
    # raises: telling a caller which check they failed is free information
    # for whoever is holding a stolen token.
    if not await revoke_refresh_token(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    access_token = create_access_token({"sub": email})
    new_refresh_token, _new_jti, new_expires_at = create_refresh_token({"sub": email})
    _set_refresh_cookie(response, new_refresh_token, new_expires_at)
    return TokenResponse(access_token=access_token)


@router.post("/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(REFRESH_COOKIE_NAME)
    if token:
        await revoke_refresh_token(token)
    response.delete_cookie(key=REFRESH_COOKIE_NAME, path="/auth")
    return {"message": "Logged out"}
