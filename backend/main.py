import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.api import auth, bots, connect, documents
from app.api.connect import reap_dead_calls_loop
from app.db.mongo import init_db
from app.db.seed import seed_fake_orders
from app.models.appointment import Appointment
from app.models.bot import Bot
from app.models.conversation import ConversationTurn
from app.models.document import Document
from app.models.order import Order
from app.models.user import User


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db([User, Bot, Document, Order, Appointment, ConversationTurn])
    await seed_fake_orders()
    # Task 2.4 — reaps finished/crashed per-call worker processes so the
    # registry and the OS process table don't grow unbounded.
    reaper_task = asyncio.create_task(reap_dead_calls_loop())
    yield
    reaper_task.cancel()


app = FastAPI(title="Voice Agent API", lifespan=lifespan)

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
