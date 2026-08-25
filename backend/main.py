from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, bots
from app.db.mongo import init_db
from app.models.bot import Bot
from app.models.user import User


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db([User, Bot])
    yield


app = FastAPI(title="Voice Agent API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(bots.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
