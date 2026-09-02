from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr

from app.core.auth import (
    REFRESH_COOKIE_NAME,
    create_access_token,
    create_refresh_token,
    revoke_refresh_token,
    verify_refresh_token,
)
from app.core.rate_limit import limiter
from app.core.security import hash_password, verify_password
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str


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
    existing = await User.find_one(User.email == body.email)
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    user = User(email=body.email, password_hash=hash_password(body.password))
    await user.insert()
    return {"message": "User created successfully"}


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
async def login(request: Request, body: LoginRequest, response: Response):
    user = await User.find_one(User.email == body.email)
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

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
    await revoke_refresh_token(token)

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
