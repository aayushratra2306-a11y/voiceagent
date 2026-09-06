import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import sentry_sdk
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from jose import JWTError, jwt
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api import approvals, auth, bot_tools, bots, connect, documents, payments, webhooks
from app.api.connect import maintain_worker_pool_loop, reap_dead_calls_loop
from app.core import health, metrics
from app.core.auth import get_current_user
from app.core.call_capacity import release_slots_from_a_previous_life
from app.core.config import settings
from app.core.rate_limit import limiter
from app.db.mongo import init_db
from app.db.seed import seed_fake_orders
from app.models.appointment import Appointment
from app.models.approval import PendingApproval
from app.models.bot import Bot
from app.models.bot_tool import BotTool
from app.models.consent import ConsentRecord
from app.models.conversation import ConversationTurn
from app.models.document import Document
from app.models.order import Order
from app.models.payment import PaymentSession
from app.models.revoked_token import RevokedRefreshToken
from app.models.user import User
from app.models.webhook import WebhookDelivery, WebhookOutboxItem, WebhookSubscription
from app.services.retention import retention_loop
from app.services.webhooks import webhook_delivery_loop

# Task 2.7 — error tracking. A blank DSN (the default — see config.py) makes
# this a confirmed no-op, verified live: no account, no behavior change,
# nothing to set up until settings.sentry_dsn is actually configured.
sentry_sdk.init(
    dsn=settings.sentry_dsn,
    traces_sample_rate=0.1,  # 10% of requests get full performance tracing
    send_default_pii=False,  # a voice-agent backend handles real customer data
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db([User, Bot, Document, Order, Appointment, ConversationTurn,
                   RevokedRefreshToken, BotTool, PaymentSession,
                   WebhookSubscription, WebhookDelivery, WebhookOutboxItem, PendingApproval,
                   ConsentRecord])
    await seed_fake_orders()
    # Task 4.5 — hand back any capacity slots this node was still holding
    # when it last stopped. Nothing is running yet, so anything tagged with
    # this node's id belongs to a call that died with the previous
    # incarnation — and task 4.7's watchdog restarts this process
    # deliberately, so that is a routine event, not an exotic one. Without
    # this, every restart would permanently cost however many calls were
    # live at the time. No-op unless Redis is configured (see
    # call_capacity.py — in-process state dies with the process anyway).
    await release_slots_from_a_previous_life()
    # Task 2.4 — reaps finished/crashed per-call worker processes so the
    # registry and the OS process table don't grow unbounded.
    reaper_task = asyncio.create_task(reap_dead_calls_loop())
    # Latency — keeps a few call workers warm so a caller does not wait
    # through a fresh interpreter importing pipecat. See the long note in
    # app/api/connect.py.
    pool_task = asyncio.create_task(maintain_worker_pool_loop())
    # Task 3.8 — durable webhook delivery. Lives in the long-lived API
    # process deliberately: an event queued from inside a call's own
    # short-lived worker process (task 2.4) needs a retry schedule that
    # can span minutes, which cannot safely live in a process that can
    # exit within seconds of the caller hanging up.
    webhook_task = asyncio.create_task(webhook_delivery_loop())
    # Task 4.7 — the watchdog half of "health checks and automatic
    # restarts". /health below answers a single request; this is what
    # notices the SAME checks failing repeatedly on their own and acts on
    # it rather than waiting for a person, or Docker's own HEALTHCHECK
    # (deploy/Dockerfile), to find out. See health.py's docstring for why
    # both exist.
    watchdog = health.Watchdog()
    app.state.watchdog = watchdog
    watchdog_task = asyncio.create_task(watchdog.run_forever())
    # Task 6.3 — expires a bot's transcripts past its own configured
    # retention period. Lives here for the same reason webhook delivery
    # does: this is the long-lived process, not a call's own short-lived
    # one (task 2.4).
    retention_task = asyncio.create_task(retention_loop())
    yield
    reaper_task.cancel()
    pool_task.cancel()
    webhook_task.cancel()
    watchdog_task.cancel()
    retention_task.cancel()


app = FastAPI(title="Voice Agent API", lifespan=lifespan)

# Task 2.5 — rate limiting (see app/core/rate_limit.py for the per-user/
# per-IP key function). SlowAPIMiddleware does the actual request-blocking;
# the exception handler turns a limit breach into a clean 429 response
# instead of an unhandled-exception 500.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(bots.router)
app.include_router(connect.router)
app.include_router(documents.router)
app.include_router(bot_tools.router)
app.include_router(payments.router)
app.include_router(webhooks.router)
app.include_router(approvals.router)


@app.get("/health")
async def health_endpoint():
    """Task 4.7 — actually verifies something (a real database ping, the
    warm pool, the circuit breakers), but says almost nothing.

    This route is PUBLIC — deploy/Caddyfile proxies it straight through to
    the internet, because Docker's HEALTHCHECK and any load balancer need
    to reach it without credentials. So the body is deliberately just a
    word, and the real answer is the status code: 200 healthy, 503 not.

    FOUND on a second read of Phase 4: this briefly returned the full
    report, which meant an anonymous request to a public URL got back the
    hostnames of every customer API that had tripped a breaker, how many
    calls were live at that moment, and the database's own error text. A
    health check has to be reachable by anyone, which is exactly why it
    must not be worth reading. The detail moved to /health/detail below.
    """
    result = await health.report()
    return JSONResponse(
        status_code=200 if result["healthy"] else 503,
        content={"status": "ok" if result["healthy"] else "degraded"},
    )


@app.get("/health/detail")
async def health_detail_endpoint(current_user: User = Depends(get_current_user)):
    """The full report — database, warm pool, capacity, every circuit
    breaker, provider fallback readiness. Behind authentication for the
    reason above: this is the operator's view, not the public one."""
    result = await health.report()
    return JSONResponse(status_code=200 if result["healthy"] else 503, content=result)


@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint(request: Request):
    """Task 4.9 — the Prometheus scrape endpoint.

    Authenticated, for the same reason /health/detail is: this reports
    which customer hostnames have tripped a breaker and how many calls are
    live right now. Either credential works —

      - a logged-in user's normal access token, so the account owner can
        just open it; or
      - settings.metrics_token as a bearer token, which is what a
        Prometheus scrape config can actually send (`authorization:
        credentials:`) since it has no way to refresh a JWT.

    Never a transcript or a caller's data either way — see metrics.py's
    module docstring for exactly what is and isn't reported here.
    """
    if not settings.metrics_enabled:
        return JSONResponse(status_code=404, content={"detail": "metrics are disabled"})

    header = request.headers.get("authorization", "")
    token = header[len("Bearer ") :] if header.startswith("Bearer ") else ""

    authorised = False
    if settings.metrics_token and token:
        # compare_digest, not ==: a plain comparison leaks the token one
        # character at a time to anyone willing to time the responses.
        authorised = secrets.compare_digest(token, settings.metrics_token)
    if not authorised and token:
        try:
            payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
            authorised = payload.get("type") == "access" and bool(payload.get("sub"))
        except JWTError:
            authorised = False

    if not authorised:
        return JSONResponse(
            status_code=401,
            content={"detail": "metrics require an access token or METRICS_TOKEN"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    body, content_type = await metrics.render()
    return Response(content=body, media_type=content_type)


@app.get("/test", response_class=HTMLResponse, include_in_schema=False)
async def test_page():
    html_path = Path(__file__).parent.parent / "test_voice.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
