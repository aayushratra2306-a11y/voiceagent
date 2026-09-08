import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from app.core.auth import get_current_user
from app.core.deps import get_owned_bot
from app.models.bot import Bot
from app.models.user import User
from app.pipeline.bot_templates import TEMPLATES

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


# Task 3.5 — the booking template's settings. Validated here rather than
# trusted, because a bad zone or an inverted pair of hours does not fail
# loudly at configuration time: it fails quietly on a call, as a caller
# being offered no slots at all or being told the wrong time of day.
def _validate_timezone(v: str) -> str:
    try:
        ZoneInfo(v)
    except Exception:
        raise ValueError(
            f"{v!r} is not a known time zone. Use an IANA name such as "
            f"Asia/Kolkata or Europe/London — not an offset like +05:30, "
            f"which cannot express daylight saving."
        ) from None
    return v


def _validate_clock(v: str) -> str:
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
        raise ValueError("Times must be 24-hour HH:MM, e.g. 09:00 or 17:30")
    return v


class BotCreate(BaseModel):
    name: str
    system_prompt: str = "You are a helpful voice assistant."
    voice_id: str = "a0e99841-438c-4a64-b679-ae501e7d6091"
    llm_model: str = "gpt-4o-mini"
    language: str = "en"
    timezone: str = "Asia/Kolkata"
    booking_open: str = "09:00"
    booking_close: str = "18:00"
    slot_minutes: int = Field(default=30, ge=5, le=480)

    @field_validator("system_prompt")
    @classmethod
    def _check_system_prompt(cls, v: str) -> str:
        return _validate_system_prompt(v)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, v: str) -> str:
        return _validate_timezone(v)

    @field_validator("booking_open", "booking_close")
    @classmethod
    def _check_clock(cls, v: str) -> str:
        return _validate_clock(v)


class BotUpdate(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    voice_id: str | None = None
    llm_model: str | None = None
    language: str | None = None
    timezone: str | None = None
    booking_open: str | None = None
    booking_close: str | None = None
    slot_minutes: int | None = Field(default=None, ge=5, le=480)

    @field_validator("system_prompt")
    @classmethod
    def _check_system_prompt(cls, v: str | None) -> str | None:
        return _validate_system_prompt(v) if v is not None else v

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, v: str | None) -> str | None:
        return _validate_timezone(v) if v is not None else v

    @field_validator("booking_open", "booking_close")
    @classmethod
    def _check_clock(cls, v: str | None) -> str | None:
        return _validate_clock(v) if v is not None else v


@router.get("/templates")
async def list_templates():
    """Task 3.9 — starting points for a new bot, not the final bot itself.

    Unauthenticated deliberately: this is the same catalogue for everyone,
    the way the list of available voices already is, and someone should be
    able to see what a new bot could look like before signing up.

    Returns the full system_prompt (not a summary) — the picker on the New
    Bot page pre-fills the actual form with it so a customer can start
    editing immediately rather than fetching it a second time.
    """
    return [t.model_dump() for t in TEMPLATES]


@router.post("/", status_code=201)
async def create_bot(body: BotCreate, current_user: User = Depends(get_current_user)):
    bot = Bot(user_id=str(current_user.id), **body.model_dump())
    await bot.insert()
    return {"id": str(bot.id), "name": bot.name}


@router.get("/")
async def list_bots(current_user: User = Depends(get_current_user)):
    # Every field the bot editor needs, not just the ones the dashboard card
    # shows. This is the only endpoint the frontend has for reading a bot —
    # BotSettingsPage loads an existing bot by finding it in this list — so a
    # field missing here is a field the edit form cannot see.
    #
    # FOUND 2026-09-04: `language` and `system_prompt` were both absent. It
    # went unnoticed while voices were one fixed English list, because nothing
    # on the page depended on the language. Once voices became per-language,
    # opening a Hindi bot showed the ENGLISH voices — `bot.language` was
    # undefined, so the UI fell back to English exactly as it would for a bot
    # with no language set. The prompt box was quietly empty for the same
    # reason. Saving did not corrupt either value only because update_bot
    # drops None fields, which is luck rather than design.
    bots = await Bot.find(Bot.user_id == str(current_user.id)).to_list()
    return [
        {
            "id": str(b.id),
            "name": b.name,
            "system_prompt": b.system_prompt,
            "llm_model": b.llm_model,
            "voice_id": b.voice_id,
            "language": b.language,
            # Task 3.5 — the bot editor is the only place these can be set,
            # and this is the only endpoint it reads a bot from. A field
            # missing here is a field the form cannot show; that exact
            # omission is what broke the language selector on 2026-09-04.
            "timezone": b.timezone,
            "booking_open": b.booking_open,
            "booking_close": b.booking_close,
            "slot_minutes": b.slot_minutes,
        }
        for b in bots
    ]


@router.patch("/{bot_id}")
async def update_bot(body: BotUpdate, bot: Bot = Depends(get_owned_bot)):
    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    await bot.set(update_data)
    return {"message": "Bot updated"}


@router.delete("/{bot_id}", status_code=204)
async def delete_bot(bot: Bot = Depends(get_owned_bot)):
    await bot.delete()
