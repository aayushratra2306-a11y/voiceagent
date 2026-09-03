import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api import auth, bots, connect, documents
from app.api.connect import maintain_worker_pool_loop, reap_dead_calls_loop
from app.core.config import settings
from app.core.rate_limit import limiter
from app.db.mongo import init_db
from app.db.seed import seed_fake_orders
from app.models.appointment import Appointment
from app.models.bot import Bot
from app.models.conversation import ConversationTurn
from app.models.document import Document
from app.models.order import Order
from app.models.revoked_token import RevokedRefreshToken
from app.models.user import User

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
    await init_db([User, Bot, Document, Order, Appointment, ConversationTurn, RevokedRefreshToken])
    await seed_fake_orders()
    # Task 2.4 — reaps finished/crashed per-call worker processes so the
    # registry and the OS process table don't grow unbounded.
    reaper_task = asyncio.create_task(reap_dead_calls_loop())
    # Latency — keeps a few call workers warm so a caller does not wait
    # through a fresh interpreter importing pipecat. See the long note in
    # app/api/connect.py.
    pool_task = asyncio.create_task(maintain_worker_pool_loop())
    yield
    reaper_task.cancel()
    pool_task.cancel()


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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/test", response_class=HTMLResponse, include_in_schema=False)
async def test_page():
    html_path = Path(__file__).parent.parent / "test_voice.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
