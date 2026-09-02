"""Task 2.5 — rate limiting. Uses slowapi's in-memory storage (its default)
rather than a Redis-backed store: this project runs as a single process
(and Task 2.4 didn't change that — call workers are separate processes, but
there's still one API server), so in-memory limits are accurate. This
stops being true the moment the API server itself runs as multiple
replicas — move to slowapi's Redis storage backend at that point, since
each replica would otherwise track its own separate counts.

One key function serves every route: per-user when the caller has a valid
access token, per-IP otherwise ("rate limit by user where you can and by IP
where you cannot" — the manual's own framing, because IP-only limiting
punishes everyone behind a shared office/campus connection along with
whoever actually misbehaved).
"""

from fastapi import Request
from jose import JWTError, jwt
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings


def rate_limit_key(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer ") :]
        try:
            payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
            if payload.get("type") == "access" and payload.get("sub"):
                return f"user:{payload['sub']}"
        except JWTError:
            pass  # falls through to IP-based limiting below
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=rate_limit_key)
