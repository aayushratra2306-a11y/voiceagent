"""Task 2.5/4.1 — rate limiting.

Storage backend is settings.redis_url-driven: blank (the default) keeps
slowapi's in-memory store, exactly as task 2.5 shipped it, which is
correct as long as this project runs as ONE API process (task 2.4's call
workers are separate processes, but there is still a single API server
counting requests). That stops being accurate the moment a second API
replica exists — each would track its own separate counts, so a limit of
5/minute would actually allow 5 per replica per minute — which is exactly
why this is one of the settings named in task 4.1's own "move each into
Redis" instruction. Sharing a redis_url with call_capacity.py (task 4.5)
and the active-call registry rather than inventing a second Redis setting:
"is there a shared cache reachable from every replica" is one fact about a
deployment, not three.

One key function serves every route: per-user when the caller has a valid
access token, per-IP otherwise ("rate limit by user where you can and by IP
where you cannot" — the manual's own framing, because IP-only limiting
punishes everyone behind a shared office/campus connection along with
whoever actually misbehaved).
"""

from fastapi import Request
from jose import JWTError, jwt
from loguru import logger
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


if settings.redis_url:
    logger.info(f"[RATE LIMIT] Using Redis storage ({settings.redis_url}) — accurate across "
                f"multiple API replicas")
    limiter = Limiter(key_func=rate_limit_key, storage_uri=settings.redis_url)
else:
    limiter = Limiter(key_func=rate_limit_key)
