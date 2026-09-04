"""Task 3.5 — the booking template.

The manual's reason for making this a template rather than one more tool is
that booking is the single most requested capability across almost every
industry, so it should be a form a customer fills in rather than a project
someone builds. What is configurable here is the bot's time zone, opening
hours and slot length; everything below is the behaviour every booking
needs and should not be rewritten per customer.

Four tools, matching the manual's steps: check availability, book,
reschedule, cancel.

Two things are worth reading closely, because they are where booking
systems actually go wrong.

**Time zones.** The manual is blunt: they will hurt you, store UTC and
convert only for display and speech. So `starts_at_utc` is the only truth,
the local wall-clock strings are a rendering of it, and every result
carries a spoken form that NAMES the zone — "three PM India time", never
just "three PM". Ambiguity here is how someone misses an appointment.

**The slot being taken between checking and booking.** This is the manual's
fourth step and it is a genuine race, not a theoretical one: two callers on
two lines, both told 3 PM is free, both booking it. Checking again before
inserting does not fix it — it just makes the window smaller. What fixes it
is a single atomic operation that only one of them can win, so booking
inserts a document into `booking_slots` whose `_id` IS the slot. MongoDB
guarantees `_id` is unique, so the second insert fails, and the caller who
lost is told the slot has just gone and offered the next ones.

That collection is used rather than a unique index on `appointments`
deliberately: adding a unique index to a collection that already holds rows
would fail at startup on the existing data, and an application that will
not boot is a worse outcome than the race.
"""

import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from app.models.appointment import Appointment
from app.pipeline import call_context

# Spoken aloud and repeated back by someone on a phone line, so the
# alphabet excludes everything that sounds like something else: B/D/E/G/P/
# T/V/Z all rhyme, M and N are routinely confused, S and F likewise, and
# O/0 and I/1 are the classic pair. What is left is short and survives a
# bad line.
_REF_ALPHABET = "AHLRUWX3467"
_REF_LENGTH = 4

# A booking further out than this is almost always the model mis-reading a
# date ("next Friday" resolved into next year), and refusing is far better
# than silently holding a slot eleven months away.
MAX_DAYS_AHEAD = 180

# Friendly names for the zones this is likely to be configured with. Only
# for speech: the model is given this so it says "India time" rather than
# reading "Asia/Kolkata" out loud, which is what it does otherwise.
_ZONE_SPOKEN = {
    "Asia/Kolkata": "India time",
    "Asia/Dubai": "Gulf time",
    "Europe/London": "UK time",
    "America/New_York": "US Eastern time",
    "America/Los_Angeles": "US Pacific time",
    "Australia/Sydney": "Sydney time",
    "UTC": "UTC",
}


class BookingConfig:
    """One bot's booking settings."""

    def __init__(self, timezone="UTC", open_time="09:00", close_time="18:00", slot_minutes=30):
        self.timezone = timezone
        self.open_time = open_time
        self.close_time = close_time
        self.slot_minutes = max(5, int(slot_minutes))

    @property
    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            # A mistyped zone must not take the tool down: UTC is wrong but
            # it is unambiguous, and the log says which bot to fix.
            logger.warning(f"[BOOKING] Unknown timezone {self.timezone!r} — falling back to UTC")
            return ZoneInfo("UTC")

    @property
    def spoken_zone(self) -> str:
        return _ZONE_SPOKEN.get(self.timezone, f"{self.timezone} time")


async def get_config() -> BookingConfig:
    """This call's booking settings, read once per call process.

    Loaded lazily rather than at call start: a bot with no booking tool
    should not pay a database round trip for settings it never uses, and
    that round trip would otherwise sit in the path before the greeting.
    """
    ctx = call_context.current()
    cached = ctx.cache.get("booking_config")
    if cached is not None:
        return cached

    config = BookingConfig()
    if ctx.bot_id:
        try:
            from beanie import PydanticObjectId

            from app.models.bot import Bot

            bot = await Bot.get(PydanticObjectId(ctx.bot_id))
            if bot:
                config = BookingConfig(
                    timezone=bot.timezone,
                    open_time=bot.booking_open,
                    close_time=bot.booking_close,
                    slot_minutes=bot.slot_minutes,
                )
        except Exception as e:
            logger.warning(f"[BOOKING] Could not read booking settings: {type(e).__name__}: {e}")

    ctx.cache["booking_config"] = config
    return config


# --- time -------------------------------------------------------------------

def _parse_local(date: str, time: str, config: BookingConfig) -> datetime | None:
    """A local wall-clock date and time as an aware datetime, or None."""
    try:
        naive = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    # Attaching the zone to a wall clock is the correct direction: this is
    # what the caller said out loud, in their own zone. On the one hour a
    # year that does not exist locally (a spring-forward gap) Python resolves
    # it to a real instant rather than raising, which is the right trade for
    # a phone call — an hour out once a year beats a crash mid-sentence.
    return naive.replace(tzinfo=config.zone)


def _spoken(local_dt: datetime, config: BookingConfig) -> str:
    """How the model should say this time, zone included.

    The zone is not optional. The manual's warning is that ambiguity here
    causes missed appointments, so every time this template hands back is
    already phrased with the zone attached.
    """
    hour = local_dt.strftime("%I").lstrip("0") or "12"
    minute = "" if local_dt.minute == 0 else f":{local_dt.minute:02d}"
    return (
        f"{hour}{minute} {local_dt.strftime('%p').lower()} on "
        f"{local_dt.strftime('%A')}, {local_dt.day} {local_dt.strftime('%B')}, "
        f"{config.spoken_zone}"
    )


def _slot_key(bot_id: str, starts_at_utc: datetime) -> str:
    """The identity of one bookable slot, for one bot.

    Includes the bot so two customers sharing this deployment never contend
    for the same slot, and is second-resolution UTC so the same instant
    always produces the same key regardless of who asked in which zone.
    """
    return f"{bot_id or 'default'}|{starts_at_utc.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}"


# --- the atomic part --------------------------------------------------------

def _slots_collection():
    from app.db.mongo import database

    return database["booking_slots"]


async def _hold_slot(key: str, purpose: str) -> bool:
    """Claim a slot, or report that someone else already has it.

    One insert, whose `_id` is the slot. Two callers racing for 3 PM both
    reach this; MongoDB lets exactly one insert succeed and rejects the
    other with a duplicate key. That is the whole mechanism, and it is
    correct across processes and machines because the guarantee is the
    database's, not this application's.
    """
    from pymongo.errors import DuplicateKeyError

    try:
        await _slots_collection().insert_one(
            {"_id": key, "purpose": purpose, "held_at": datetime.now(UTC)}
        )
        return True
    except DuplicateKeyError:
        logger.info(f"[BOOKING] Slot {key} was already taken")
        return False


async def _release_slot(key: str) -> None:
    if not key:
        return
    try:
        await _slots_collection().delete_one({"_id": key})
    except Exception as e:
        # A slot left held is a slot nobody can book — worth an error,
        # because it needs a human to clear it.
        logger.error(f"[BOOKING] Could not release slot {key}: {type(e).__name__}: {e}")


async def _taken_keys(bot_id: str, day_start: datetime, day_end: datetime) -> set[str]:
    """Which slots on this day are already held."""
    prefix = f"{bot_id or 'default'}|"
    keys: set[str] = set()
    try:
        cursor = _slots_collection().find(
            {
                "_id": {
                    "$gte": f"{prefix}{day_start.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}",
                    "$lte": f"{prefix}{day_end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}",
                }
            }
        )
        async for doc in cursor:
            keys.add(doc["_id"])
    except Exception as e:
        logger.warning(f"[BOOKING] Could not read held slots: {type(e).__name__}: {e}")
    return keys


async def _new_reference() -> str:
    """A short code the caller can repeat back, checked for collisions."""
    for _ in range(8):
        ref = "".join(random.choice(_REF_ALPHABET) for _ in range(_REF_LENGTH))
        if not await Appointment.find_one(Appointment.reference == ref, Appointment.status == "booked"):
            return ref
    # Astronomically unlikely; a longer code is still better than a clash.
    return "".join(random.choice(_REF_ALPHABET) for _ in range(_REF_LENGTH + 2))


# --- tools ------------------------------------------------------------------

async def check_availability(params: FunctionCallParams, date: str):
    """Find which appointment times are still free on a given day.

    Call this BEFORE booking whenever the caller asks what is available, or
    names a day without a time. Offer the caller two or three of the times
    this returns rather than reading the whole list out loud.

    Args:
        date: The day to check, as YYYY-MM-DD. Work the exact date out
            yourself from what the caller said ("tomorrow", "next Tuesday")
            using get_current_datetime if you need today's date — never pass
            a relative phrase through.
    """
    config = await get_config()
    ctx = call_context.current()
    logger.info(f"[BOOKING] check_availability date={date!r}")

    first = _parse_local(date, config.open_time, config)
    last = _parse_local(date, config.close_time, config)
    if first is None or last is None:
        await params.result_callback({
            "ok": False,
            "message": "That date was not a valid YYYY-MM-DD date. Work it out and try again.",
        })
        return

    now = datetime.now(UTC)
    if (first.astimezone(UTC) - now).days > MAX_DAYS_AHEAD:
        await params.result_callback({
            "ok": False,
            "message": (
                f"That date is more than {MAX_DAYS_AHEAD} days away, which is probably not "
                f"what the caller meant. Confirm the date with them."
            ),
        })
        return

    taken = await _taken_keys(ctx.bot_id or "", first, last)

    free: list[dict] = []
    cursor = first
    while cursor < last:
        starts_utc = cursor.astimezone(UTC)
        key = _slot_key(ctx.bot_id or "", starts_utc)
        # A slot that has already passed is not available, however empty the
        # calendar looks. Callers do ask for "today at nine" at eleven.
        if key not in taken and starts_utc > now:
            free.append({
                "time": cursor.strftime("%H:%M"),
                "spoken": _spoken(cursor, config),
            })
        cursor += timedelta(minutes=config.slot_minutes)

    logger.info(f"[BOOKING] check_availability -> {len(free)} free slot(s) on {date}")
    await params.result_callback({
        "ok": True,
        "date": date,
        "timezone": config.timezone,
        "spoken_timezone": config.spoken_zone,
        "free_slots": free[:24],
        "message": (
            f"There is nothing free on {date}. Offer the caller another day."
            if not free
            else "Offer two or three of these times, and always say the time zone with them."
        ),
    })


async def book_appointment(params: FunctionCallParams, date: str, time: str, purpose: str, caller_name: str = ""):
    """Book an appointment slot for the caller.

    Confirm the date, time and purpose back to the caller in your own words
    and read the reference code out clearly — they need it to change or
    cancel. Always say the time zone with the time. If this reports the slot
    is taken, say so plainly and offer one of the alternatives it returns;
    never claim a booking that did not happen.

    Args:
        date: The appointment date as YYYY-MM-DD. Work out the exact date
            yourself from what the caller said — do not pass "tomorrow".
        time: The time in 24-hour HH:MM, in the caller's local time, e.g.
            "15:00" for three in the afternoon.
        purpose: A short description of what the appointment is for.
        caller_name: The caller's name if they have given it. Leave empty
            if they have not — do not invent one.
    """
    config = await get_config()
    ctx = call_context.current()
    logger.info(f"[BOOKING] book_appointment date={date!r} time={time!r} purpose={purpose!r}")

    local = _parse_local(date, time, config)
    if local is None:
        await params.result_callback({
            "booked": False,
            "message": (
                "The date or time was not in a valid format. Work out the date as "
                "YYYY-MM-DD and the time as 24-hour HH:MM, then try again."
            ),
        })
        return

    starts_utc = local.astimezone(UTC)
    now = datetime.now(UTC)
    if starts_utc <= now:
        await params.result_callback({
            "booked": False,
            "message": "That time has already passed. Ask the caller for a later time.",
        })
        return
    if (starts_utc - now).days > MAX_DAYS_AHEAD:
        await params.result_callback({
            "booked": False,
            "message": f"That is more than {MAX_DAYS_AHEAD} days away. Confirm the date with the caller.",
        })
        return

    key = _slot_key(ctx.bot_id or "", starts_utc)
    if not await _hold_slot(key, purpose):
        # The manual's fourth step. Losing the race is not an error — it is
        # a normal outcome on a busy calendar — so the caller gets the next
        # times rather than an apology with nothing after it.
        alternatives = await _next_free(local, config, ctx.bot_id or "", limit=3)
        await params.result_callback({
            "booked": False,
            "reason": "slot_taken",
            "alternatives": alternatives,
            "message": (
                "That slot has just been taken. Tell the caller plainly and offer one "
                "of the alternative times — say the time zone with each."
            ),
        })
        return

    reference = await _new_reference()
    appointment = Appointment(
        date=local.strftime("%Y-%m-%d"),
        time=local.strftime("%H:%M"),
        purpose=purpose,
        booked_by=caller_name or "voice caller",
        caller_name=caller_name,
        starts_at_utc=starts_utc,
        timezone=config.timezone,
        duration_minutes=config.slot_minutes,
        reference=reference,
        status="booked",
        bot_id=ctx.bot_id or "",
        slot_key=key,
    )
    try:
        await appointment.insert()
    except Exception as e:
        # The hold succeeded and the record did not. Releasing it is the
        # whole point: otherwise that slot is blocked forever by a booking
        # that does not exist.
        await _release_slot(key)
        logger.error(f"[BOOKING] Slot held but appointment not saved: {type(e).__name__}: {e}")
        await params.result_callback({
            "booked": False,
            "message": "The booking could not be saved. Tell the caller it did not go through.",
        })
        return

    logger.info(f"[BOOKING] Booked {reference} at {starts_utc.isoformat()} ({config.timezone})")
    await _emit("appointment.booked", appointment)

    await params.result_callback({
        "booked": True,
        "reference": reference,
        "date": appointment.date,
        "time": appointment.time,
        "timezone": config.timezone,
        "spoken_time": _spoken(local, config),
        "purpose": purpose,
        "message": (
            "Confirm this back to the caller including the time zone, then read the "
            "reference code out one character at a time and ask them to write it down."
        ),
    })


async def _next_free(after: datetime, config: BookingConfig, bot_id: str, limit: int = 3) -> list[dict]:
    """The next few free slots at or after a given local time.

    Searches the rest of that day and then the following one, which is what
    a caller who has just lost a slot actually wants to hear.
    """
    out: list[dict] = []
    now = datetime.now(UTC)
    for day_offset in (0, 1, 2):
        day = (after + timedelta(days=day_offset)).strftime("%Y-%m-%d")
        first = _parse_local(day, config.open_time, config)
        last = _parse_local(day, config.close_time, config)
        if first is None or last is None:
            continue
        taken = await _taken_keys(bot_id, first, last)
        cursor = max(first, after + timedelta(minutes=config.slot_minutes)) if day_offset == 0 else first
        while cursor < last and len(out) < limit:
            starts_utc = cursor.astimezone(UTC)
            if _slot_key(bot_id, starts_utc) not in taken and starts_utc > now:
                out.append({
                    "date": cursor.strftime("%Y-%m-%d"),
                    "time": cursor.strftime("%H:%M"),
                    "spoken": _spoken(cursor, config),
                })
            cursor += timedelta(minutes=config.slot_minutes)
        if len(out) >= limit:
            break
    return out


async def _find_booking(reference: str, bot_id: str) -> Appointment | None:
    ref = (reference or "").strip().upper().replace(" ", "").replace("-", "")
    if not ref:
        return None
    return await Appointment.find_one(
        Appointment.reference == ref,
        Appointment.status == "booked",
    )


async def cancel_appointment(params: FunctionCallParams, reference: str):
    """Cancel an existing appointment using the caller's reference code.

    Ask the caller to read their reference code back before calling this,
    and confirm what you are about to cancel — the date, the time and what
    it was for — so a wrong code does not silently cancel the wrong booking.

    Args:
        reference: The short reference code the caller was given when they
            booked, e.g. "AH34". Pass it exactly as they said it.
    """
    ctx = call_context.current()
    config = await get_config()
    logger.info(f"[BOOKING] cancel_appointment reference={reference!r}")

    booking = await _find_booking(reference, ctx.bot_id or "")
    if booking is None:
        await params.result_callback({
            "cancelled": False,
            "message": (
                "No live booking has that reference. Ask the caller to read the code "
                "again, character by character — do not guess at a correction."
            ),
        })
        return

    booking.status = "cancelled"
    await booking.save()
    await _release_slot(booking.slot_key)
    logger.info(f"[BOOKING] Cancelled {booking.reference}")
    await _emit("appointment.cancelled", booking)

    await params.result_callback({
        "cancelled": True,
        "reference": booking.reference,
        "date": booking.date,
        "time": booking.time,
        "purpose": booking.purpose,
        "spoken_time": (
            _spoken(_parse_local(booking.date, booking.time, config), config)
            if _parse_local(booking.date, booking.time, config)
            else f"{booking.time} on {booking.date}"
        ),
        "message": "Tell the caller exactly what has been cancelled, including the day and time.",
    })


async def reschedule_appointment(params: FunctionCallParams, reference: str, date: str, time: str):
    """Move an existing appointment to a different day or time.

    Confirm both the old time and the new one back to the caller. If the new
    slot turns out to be taken, the original booking is left exactly as it
    was — say so, so the caller knows they have not lost it.

    Args:
        reference: The caller's existing reference code, e.g. "AH34".
        date: The new date as YYYY-MM-DD.
        time: The new time in 24-hour HH:MM, caller's local time.
    """
    config = await get_config()
    ctx = call_context.current()
    logger.info(f"[BOOKING] reschedule reference={reference!r} -> {date!r} {time!r}")

    booking = await _find_booking(reference, ctx.bot_id or "")
    if booking is None:
        await params.result_callback({
            "rescheduled": False,
            "message": "No live booking has that reference. Ask the caller to read the code again.",
        })
        return

    local = _parse_local(date, time, config)
    if local is None:
        await params.result_callback({
            "rescheduled": False,
            "message": "The new date or time was not valid. Work them out again and retry.",
        })
        return

    starts_utc = local.astimezone(UTC)
    if starts_utc <= datetime.now(UTC):
        await params.result_callback({
            "rescheduled": False,
            "message": "That time has already passed. Ask for a later one. The original booking still stands.",
        })
        return

    new_key = _slot_key(ctx.bot_id or "", starts_utc)
    old_key = booking.slot_key

    if new_key == old_key:
        await params.result_callback({
            "rescheduled": False,
            "message": "That is the time the booking is already at. Nothing has changed.",
        })
        return

    # The new slot is claimed BEFORE the old one is released. The other
    # order would hand the caller's own slot to somebody else in the moment
    # between, and leave them with nothing if the new slot then failed.
    if not await _hold_slot(new_key, booking.purpose):
        alternatives = await _next_free(local, config, ctx.bot_id or "", limit=3)
        await params.result_callback({
            "rescheduled": False,
            "reason": "slot_taken",
            "alternatives": alternatives,
            "message": (
                "That time is taken. The original booking is UNCHANGED and still stands — "
                "say that clearly, then offer one of the alternatives."
            ),
        })
        return

    old_spoken_local = _parse_local(booking.date, booking.time, config)
    booking.date = local.strftime("%Y-%m-%d")
    booking.time = local.strftime("%H:%M")
    booking.starts_at_utc = starts_utc
    booking.timezone = config.timezone
    booking.slot_key = new_key
    try:
        await booking.save()
    except Exception as e:
        await _release_slot(new_key)
        logger.error(f"[BOOKING] Reschedule failed after holding slot: {type(e).__name__}: {e}")
        await params.result_callback({
            "rescheduled": False,
            "message": "The change could not be saved. The original booking still stands.",
        })
        return

    await _release_slot(old_key)
    logger.info(f"[BOOKING] Rescheduled {booking.reference} to {starts_utc.isoformat()}")
    await _emit("appointment.rescheduled", booking)

    await params.result_callback({
        "rescheduled": True,
        "reference": booking.reference,
        "from": _spoken(old_spoken_local, config) if old_spoken_local else "the previous time",
        "to": _spoken(local, config),
        "message": "Confirm the new day and time back to the caller, with the time zone.",
    })


async def _emit(event: str, appointment: Appointment) -> None:
    """Tell the customer's own system that this happened.

    The delivery mechanism is task 3.8, which is not built yet — hence the
    ImportError branch, which is a deliberate no-op rather than a warning:
    until webhooks exist, having nothing to notify is the correct state and
    should not put a line in the log on every booking. The call sites are
    written now so 3.8 is a wiring change in one file rather than a hunt
    through this one.

    Once it does exist it stays wrapped, because a webhook is a
    notification: a notification failing must never turn into a booking
    failing.
    """
    try:
        from app.services.webhooks import emit  # noqa: PLC0415
    except ImportError:
        return

    try:
        ctx = call_context.current()
        await emit(
            event,
            user_id=ctx.user_id,
            payload={
                "reference": appointment.reference,
                "date": appointment.date,
                "time": appointment.time,
                "timezone": appointment.timezone,
                "starts_at_utc": (
                    appointment.starts_at_utc.isoformat() if appointment.starts_at_utc else None
                ),
                "purpose": appointment.purpose,
                "caller_name": appointment.caller_name,
                "bot_id": appointment.bot_id,
                "status": appointment.status,
            },
        )
    except Exception as e:
        logger.warning(f"[BOOKING] Could not emit {event}: {type(e).__name__}: {e}")


BOOKING_TOOLS = [
    check_availability,
    book_appointment,
    cancel_appointment,
    reschedule_appointment,
]
