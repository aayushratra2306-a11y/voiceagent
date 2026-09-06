import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from app.core.auth import get_current_user
from app.core.deps import get_owned_bot
from app.core.redaction import ALL_KINDS as _REDACTION_KINDS
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


# Task 6.2 — rejected rather than silently ignored. A typo'd kind
# ("crd" for "card") that was quietly dropped would leave a customer
# believing card numbers are being redacted when nothing matches that name
# at all — a compliance control that looks configured and isn't is worse
# than one that plainly refuses the bad input.
def _validate_redaction_kinds(v: list[str]) -> list[str]:
    unknown = sorted(set(v) - _REDACTION_KINDS)
    if unknown:
        raise ValueError(
            f"Unknown redaction categories: {unknown}. Valid values are "
            f"{sorted(_REDACTION_KINDS)}."
        )
    return v


# Task 6.3 — this is SPOKEN aloud at the start of every call, so a limit
# far below system_prompt's 4000 chars: a legal disclosure is a sentence or
# two, and anything long enough to need thousands of characters is a
# document to link to, not a line to read into someone's ear before they
# have even said hello.
MAX_CONSENT_ANNOUNCEMENT_LENGTH = 600


def _validate_consent_announcement(v: str) -> str:
    if len(v) > MAX_CONSENT_ANNOUNCEMENT_LENGTH:
        raise ValueError(
            f"Consent announcement too long (max {MAX_CONSENT_ANNOUNCEMENT_LENGTH} "
            f"characters) — it is spoken at the start of every call."
        )
    return v


# Task 6.1 — bounded on both count and length. Every topic is matched
# case-insensitively against every sentence of every reply (see
# guardrails.check_output, which anchors each one to whole words), so an
# unbounded list would mean an unbounded number of regex searches on the
# hot path of every single call.
MAX_GUARDRAIL_TOPICS = 25
MAX_GUARDRAIL_TOPIC_LENGTH = 100


def _validate_guardrail_topics(v: list[str]) -> list[str]:
    if len(v) > MAX_GUARDRAIL_TOPICS:
        raise ValueError(f"Too many guardrail topics (max {MAX_GUARDRAIL_TOPICS})")
    cleaned = [t.strip() for t in v]
    for topic in cleaned:
        if not topic:
            raise ValueError("A guardrail topic cannot be blank")
        if len(topic) > MAX_GUARDRAIL_TOPIC_LENGTH:
            raise ValueError(
                f"Guardrail topic {topic[:40]!r}... exceeds "
                f"{MAX_GUARDRAIL_TOPIC_LENGTH} characters"
            )
    return cleaned


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
    # Task 6.2 — defaults to every category. See models/bot.py's own note:
    # an operator who has not thought about this should get the safe
    # answer, not silent plaintext card numbers until they opt in.
    redact_transcripts: list[str] = Field(default_factory=lambda: sorted(_REDACTION_KINDS))
    # Task 6.3. See models/bot.py for why these default the way they do:
    # recording_enabled=True matches what every bot has always done since
    # task 1.5 (transcripts are already saved) — the change here is
    # disclosure, not a new decision to start recording.
    recording_enabled: bool = True
    consent_announcement: str = (
        "This call may be recorded for quality and training purposes."
    )
    recording_retention_days: int = Field(default=0, ge=0)
    # Task 6.1 — empty by default. See models/bot.py: the universal rules
    # every bot gets (never reveal instructions, never give medical/legal
    # advice, ...) apply regardless; this is what a customer opts INTO for
    # topics specific to their own business.
    guardrail_topics: list[str] = Field(default_factory=list)

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

    @field_validator("redact_transcripts")
    @classmethod
    def _check_redaction_kinds(cls, v: list[str]) -> list[str]:
        return _validate_redaction_kinds(v)

    @field_validator("consent_announcement")
    @classmethod
    def _check_consent_announcement(cls, v: str) -> str:
        return _validate_consent_announcement(v)

    @field_validator("guardrail_topics")
    @classmethod
    def _check_guardrail_topics(cls, v: list[str]) -> list[str]:
        return _validate_guardrail_topics(v)


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
    redact_transcripts: list[str] | None = None
    recording_enabled: bool | None = None
    consent_announcement: str | None = None
    recording_retention_days: int | None = Field(default=None, ge=0)
    guardrail_topics: list[str] | None = None

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

    @field_validator("redact_transcripts")
    @classmethod
    def _check_redaction_kinds(cls, v: list[str] | None) -> list[str] | None:
        return _validate_redaction_kinds(v) if v is not None else v

    @field_validator("consent_announcement")
    @classmethod
    def _check_consent_announcement(cls, v: str | None) -> str | None:
        return _validate_consent_announcement(v) if v is not None else v

    @field_validator("guardrail_topics")
    @classmethod
    def _check_guardrail_topics(cls, v: list[str] | None) -> list[str] | None:
        return _validate_guardrail_topics(v) if v is not None else v


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
            "redact_transcripts": b.redact_transcripts,
            "recording_enabled": b.recording_enabled,
            "consent_announcement": b.consent_announcement,
            "recording_retention_days": b.recording_retention_days,
            "guardrail_topics": b.guardrail_topics,
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
