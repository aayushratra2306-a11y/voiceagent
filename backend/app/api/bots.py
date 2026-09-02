import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from app.core.auth import get_current_user
from app.core.deps import get_owned_bot
from app.models.bot import Bot
from app.models.user import User

router = APIRouter(prefix="/bots", tags=["bots"])

# Task 2.6 — bot-instruction validation. Two separate defenses:
#  1. A hard length cap. Not a security measure by itself, but it bounds
#     how much room an attack string has to work with, and keeps one bot's
#     prompt from silently ballooning token cost on every single turn.
#  2. A pattern blocklist for known prompt-injection/jailbreak phrasing.
#
# Labeled honestly: this catches known, literal phrasings — it is defense
# in depth, not a solved problem. Prompt injection in general is an open
# research problem; no regex list makes a bot's instructions un-hijackable.
# What this DOES reliably stop is the common, copy-pasted attack strings
# ("ignore previous instructions", "reveal your system prompt", etc.) that
# make up the overwhelming majority of real-world attempts, at essentially
# zero cost and with no false-positive risk to a legitimate instruction set.
MAX_SYSTEM_PROMPT_LENGTH = 4000

_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"ignore (all |any )?(the |your )?(previous|prior|above|earlier) instructions",
        r"disregard (all |any )?(the |your )?(previous|prior|above|earlier) instructions",
        r"forget (all |any )?(the |your )?(previous|prior|above|earlier) instructions",
        r"you are now (DAN|in developer mode|jailbroken|unrestricted)",
        r"reveal (your |the )?(system prompt|hidden instructions|internal instructions)",
        r"print (your |the )?(system prompt|hidden instructions|internal instructions)",
        r"repeat (your |the )?(system prompt|instructions|everything above) (verbatim|exactly)",
        r"what (is|are) your (system prompt|hidden instructions)",
        r"act as if you have no (restrictions|rules|guidelines|limits)",
        r"pretend (you have no|there are no) (restrictions|rules|guidelines|limits)",
    ]
]


def _validate_system_prompt(v: str) -> str:
    if len(v) > MAX_SYSTEM_PROMPT_LENGTH:
        raise ValueError(f"System prompt too long (max {MAX_SYSTEM_PROMPT_LENGTH} characters)")
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(v):
            raise ValueError(
                "System prompt contains a disallowed instruction-override phrase. "
                "Rewrite it to describe what the bot should do, not to reference "
                "or override its own instructions."
            )
    return v


class BotCreate(BaseModel):
    name: str
    system_prompt: str = "You are a helpful voice assistant."
    voice_id: str = "a0e99841-438c-4a64-b679-ae501e7d6091"
    llm_model: str = "gpt-4o-mini"
    language: str = "en"

    @field_validator("system_prompt")
    @classmethod
    def _check_system_prompt(cls, v: str) -> str:
        return _validate_system_prompt(v)


class BotUpdate(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    voice_id: str | None = None
    llm_model: str | None = None
    language: str | None = None

    @field_validator("system_prompt")
    @classmethod
    def _check_system_prompt(cls, v: str | None) -> str | None:
        return _validate_system_prompt(v) if v is not None else v


@router.post("/", status_code=201)
async def create_bot(body: BotCreate, current_user: User = Depends(get_current_user)):
    bot = Bot(user_id=str(current_user.id), **body.model_dump())
    await bot.insert()
    return {"id": str(bot.id), "name": bot.name}


@router.get("/")
async def list_bots(current_user: User = Depends(get_current_user)):
    bots = await Bot.find(Bot.user_id == str(current_user.id)).to_list()
    return [{"id": str(b.id), "name": b.name, "llm_model": b.llm_model, "voice_id": b.voice_id} for b in bots]


@router.patch("/{bot_id}")
async def update_bot(body: BotUpdate, bot: Bot = Depends(get_owned_bot)):
    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    await bot.set(update_data)
    return {"message": "Bot updated"}


@router.delete("/{bot_id}", status_code=204)
async def delete_bot(bot: Bot = Depends(get_owned_bot)):
    await bot.delete()
