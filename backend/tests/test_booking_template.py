"""Task 3.5 — the booking template.

The two things these tests exist for are the two the manual singles out.

Time zones: it says they will hurt you, store UTC, convert only for display
and speech, and always say the zone out loud. So there are tests that the
stored instant is right, that the spoken form names the zone, and that the
same wall-clock time in two different zones is two different instants.

The slot being taken between checking and booking: it says handle it, and
it is a real race rather than a theoretical one — two callers on two lines,
both told 3 PM is free. The tests here book the same slot twice, race two
bookings concurrently, and check that the loser is told plainly and offered
somewhere else to go rather than being handed a booking that does not
exist.

Plus the case that is easy to get backwards: a reschedule onto a slot that
turns out to be taken must leave the ORIGINAL booking exactly as it was.
Releasing the old slot first would give the caller's own appointment away
and then fail, which is worse than not moving it at all.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models.appointment import Appointment
from app.models.bot import Bot
from app.pipeline import booking, call_context

pytestmark = pytest.mark.asyncio(loop_scope="session")

KOLKATA = ZoneInfo("Asia/Kolkata")


class _Params:
    """Stands in for pipecat's FunctionCallParams."""

    def __init__(self):
        self.result = None

    async def result_callback(self, result):
        self.result = result


async def _call(fn, **kwargs):
    params = _Params()
    await fn(params, **kwargs)
    return params.result


def _future_date(days: int = 3) -> str:
    """A date far enough ahead that the slot is never already past."""
    return (datetime.now(KOLKATA) + timedelta(days=days)).strftime("%Y-%m-%d")


@pytest.fixture
async def _a_bot_on_a_call():
    """A bot with known booking settings, and a clean calendar.

    Per-test, not autouse: most tests need one, but the reference-alphabet
    and wiring checks do not and would break if this set call_context."""
    bot = Bot(
        user_id="booking-tests",
        name="Booking test bot",
        timezone="Asia/Kolkata",
        booking_open="09:00",
        booking_close="17:00",
        slot_minutes=30,
    )
    await bot.insert()
    call_context.set_call(bot_id=str(bot.id), session_id="s1")

    from app.db.mongo import database
    await database["booking_slots"].delete_many({})

    yield bot

    await Appointment.find(Appointment.bot_id == str(bot.id)).delete()
    await database["booking_slots"].delete_many({"_id": {"$regex": f"^{bot.id}\\|"}})
    await bot.delete()
    call_context.clear()


# --- time zones -------------------------------------------------------------

async def test_a_local_time_is_stored_as_the_right_utc_instant(_a_bot_on_a_call):  # noqa: ARG001
    """09:00 in Kolkata is 03:30 UTC. Getting this wrong by 5.5 hours is
    the failure the manual's 'store UTC' rule exists to prevent."""
    date = _future_date()
    result = await _call(booking.book_appointment, date=date, time="09:00", purpose="checkup")

    assert result["booked"] is True, result
    saved = await Appointment.find_one(Appointment.reference == result["reference"])
    assert saved.starts_at_utc.astimezone(UTC).strftime("%H:%M") == "03:30"
    # And the local wall clock is kept alongside it for speaking back.
    assert saved.time == "09:00"
    assert saved.timezone == "Asia/Kolkata"


async def test_the_same_wall_clock_in_two_zones_is_two_different_instants():
    """The whole reason an offset-free IANA name is stored rather than a
    number: 10:00 means different moments in different places."""
    date = _future_date()
    config_in = booking.BookingConfig(timezone="Asia/Kolkata")
    config_lon = booking.BookingConfig(timezone="Europe/London")

    in_utc = booking._parse_local(date, "10:00", config_in).astimezone(UTC)
    lon_utc = booking._parse_local(date, "10:00", config_lon).astimezone(UTC)
    assert in_utc != lon_utc


async def test_the_spoken_time_always_names_the_zone(_a_bot_on_a_call):  # noqa: ARG001
    """The manual: ambiguity here causes missed appointments."""
    date = _future_date()
    result = await _call(booking.book_appointment, date=date, time="15:00", purpose="review")

    spoken = result["spoken_time"]
    assert "India time" in spoken, spoken
    assert "3 pm" in spoken.lower(), spoken


def test_an_unknown_zone_falls_back_to_utc_rather_than_raising():
    """A mistyped zone must not take a live call's booking tool down."""
    assert booking.BookingConfig(timezone="Mars/Olympus").zone == ZoneInfo("UTC")


async def test_the_bots_own_settings_are_used(_a_bot_on_a_call):  # noqa: ARG001
    config = await booking.get_config()
    assert config.timezone == "Asia/Kolkata"
    assert config.slot_minutes == 30
    assert config.close_time == "17:00"


# --- the race ---------------------------------------------------------------

async def test_booking_the_same_slot_twice_is_refused_not_double_booked(_a_bot_on_a_call):  # noqa: ARG001
    date = _future_date()
    first = await _call(booking.book_appointment, date=date, time="11:00", purpose="one")
    second = await _call(booking.book_appointment, date=date, time="11:00", purpose="two")

    assert first["booked"] is True
    assert second["booked"] is False
    assert second["reason"] == "slot_taken"


async def test_the_caller_who_loses_the_slot_is_offered_alternatives(_a_bot_on_a_call):  # noqa: ARG001
    """Losing a slot is normal on a busy calendar — an apology with nothing
    after it is not a useful thing to say to someone on a phone."""
    date = _future_date()
    await _call(booking.book_appointment, date=date, time="11:00", purpose="one")
    second = await _call(booking.book_appointment, date=date, time="11:00", purpose="two")

    assert second["alternatives"], second
    assert all("spoken" in a and "India time" in a["spoken"] for a in second["alternatives"])


async def test_two_simultaneous_bookings_produce_exactly_one_appointment(_a_bot_on_a_call):  # noqa: ARG001
    """The actual race, run concurrently rather than in sequence. Checking
    availability again before inserting would still fail this; only the
    atomic insert makes it pass."""
    date = _future_date()
    results = await asyncio.gather(
        _call(booking.book_appointment, date=date, time="12:00", purpose="a"),
        _call(booking.book_appointment, date=date, time="12:00", purpose="b"),
    )

    booked = [r for r in results if r.get("booked")]
    assert len(booked) == 1, results
    saved = await Appointment.find(
        Appointment.date == date, Appointment.time == "12:00", Appointment.status == "booked"
    ).to_list()
    assert len(saved) == 1


# --- what must be refused ---------------------------------------------------

async def test_a_time_in_the_past_is_refused(_a_bot_on_a_call):  # noqa: ARG001
    yesterday = (datetime.now(KOLKATA) - timedelta(days=1)).strftime("%Y-%m-%d")
    result = await _call(booking.book_appointment, date=yesterday, time="10:00", purpose="x")

    assert result["booked"] is False
    assert "passed" in result["message"]


async def test_a_date_absurdly_far_ahead_is_refused(_a_bot_on_a_call):  # noqa: ARG001
    """Almost always the model mis-reading a relative date, and silently
    holding a slot eleven months out is worse than asking again."""
    far = (datetime.now(KOLKATA) + timedelta(days=booking.MAX_DAYS_AHEAD + 30)).strftime("%Y-%m-%d")
    result = await _call(booking.book_appointment, date=far, time="10:00", purpose="x")

    assert result["booked"] is False


async def test_a_malformed_date_is_refused_without_touching_the_database(_a_bot_on_a_call):  # noqa: ARG001
    result = await _call(booking.book_appointment, date="next tuesday", time="10:00", purpose="x")

    assert result["booked"] is False
    assert await Appointment.find_one(Appointment.purpose == "x") is None


# --- availability -------------------------------------------------------------

async def test_availability_lists_slots_and_excludes_the_booked_one(_a_bot_on_a_call):  # noqa: ARG001
    date = _future_date()
    before = await _call(booking.check_availability, date=date)
    await _call(booking.book_appointment, date=date, time="14:00", purpose="taken")
    after = await _call(booking.check_availability, date=date)

    assert "14:00" in [s["time"] for s in before["free_slots"]]
    assert "14:00" not in [s["time"] for s in after["free_slots"]]


async def test_availability_stops_at_the_closing_time(_a_bot_on_a_call):  # noqa: ARG001
    """close_time is exclusive: the last slot STARTS before it. A 17:00
    close with 30-minute slots must not offer 17:00."""
    result = await _call(booking.check_availability, date=_future_date())
    times = [s["time"] for s in result["free_slots"]]

    assert "16:30" in times
    assert "17:00" not in times


async def test_availability_does_not_offer_a_slot_that_has_already_passed(_a_bot_on_a_call):  # noqa: ARG001
    """Callers do ask for 'today at nine' at eleven o'clock."""
    today = datetime.now(KOLKATA).strftime("%Y-%m-%d")
    result = await _call(booking.check_availability, date=today)

    now_local = datetime.now(KOLKATA)
    for slot in result["free_slots"]:
        hour, minute = (int(x) for x in slot["time"].split(":"))
        assert (hour, minute) > (now_local.hour, now_local.minute)


# --- cancelling ---------------------------------------------------------------

async def test_cancelling_frees_the_slot_for_someone_else(_a_bot_on_a_call):  # noqa: ARG001
    date = _future_date()
    first = await _call(booking.book_appointment, date=date, time="10:00", purpose="one")
    await _call(booking.cancel_appointment, reference=first["reference"])
    second = await _call(booking.book_appointment, date=date, time="10:00", purpose="two")

    assert second["booked"] is True, second


async def test_cancelling_an_unknown_reference_cancels_nothing(_a_bot_on_a_call):  # noqa: ARG001
    date = _future_date()
    made = await _call(booking.book_appointment, date=date, time="10:00", purpose="one")
    result = await _call(booking.cancel_appointment, reference="XXXX")

    assert result["cancelled"] is False
    still = await Appointment.find_one(Appointment.reference == made["reference"])
    assert still.status == "booked"


async def test_a_cancellation_says_what_was_cancelled(_a_bot_on_a_call):  # noqa: ARG001
    """So the caller can catch it if the wrong code was heard."""
    date = _future_date()
    made = await _call(booking.book_appointment, date=date, time="10:00", purpose="dentist")
    result = await _call(booking.cancel_appointment, reference=made["reference"])

    assert result["purpose"] == "dentist"
    assert "India time" in result["spoken_time"]


# --- rescheduling ---------------------------------------------------------------

async def test_rescheduling_moves_the_booking_and_frees_the_old_slot(_a_bot_on_a_call):  # noqa: ARG001
    date = _future_date()
    made = await _call(booking.book_appointment, date=date, time="10:00", purpose="one")
    moved = await _call(booking.reschedule_appointment, reference=made["reference"], date=date, time="15:30")

    assert moved["rescheduled"] is True, moved
    saved = await Appointment.find_one(Appointment.reference == made["reference"])
    assert saved.time == "15:30"
    # The old slot is free again.
    other = await _call(booking.book_appointment, date=date, time="10:00", purpose="someone else")
    assert other["booked"] is True


async def test_a_reschedule_onto_a_taken_slot_leaves_the_original_standing(_a_bot_on_a_call):  # noqa: ARG001
    """The case that is easy to get backwards. Releasing the old slot first
    would give the caller's own appointment away and then fail."""
    date = _future_date()
    mine = await _call(booking.book_appointment, date=date, time="10:00", purpose="mine")
    await _call(booking.book_appointment, date=date, time="16:00", purpose="theirs")

    result = await _call(booking.reschedule_appointment, reference=mine["reference"], date=date, time="16:00")

    assert result["rescheduled"] is False
    assert result["reason"] == "slot_taken"
    assert "UNCHANGED" in result["message"]
    saved = await Appointment.find_one(Appointment.reference == mine["reference"])
    assert saved.time == "10:00" and saved.status == "booked"


async def test_rescheduling_reports_both_the_old_time_and_the_new_one(_a_bot_on_a_call):  # noqa: ARG001
    date = _future_date()
    made = await _call(booking.book_appointment, date=date, time="10:00", purpose="one")
    moved = await _call(booking.reschedule_appointment, reference=made["reference"], date=date, time="15:30")

    assert "10 am" in moved["from"].lower()
    assert "3:30 pm" in moved["to"].lower()
    assert "India time" in moved["to"]


async def test_rescheduling_an_unknown_reference_changes_nothing(_a_bot_on_a_call):  # noqa: ARG001
    result = await _call(
        booking.reschedule_appointment, reference="ZZZZ", date=_future_date(), time="10:00"
    )
    assert result["rescheduled"] is False


# --- the reference code -------------------------------------------------------

def test_the_reference_alphabet_has_nothing_that_sounds_alike():
    """It is read out and repeated back over a phone line."""
    for confusable in "BDEGPTVZCMNSIO01":
        assert confusable not in booking._REF_ALPHABET, confusable


async def test_a_reference_is_issued_and_can_be_used_again(_a_bot_on_a_call):  # noqa: ARG001
    date = _future_date()
    made = await _call(booking.book_appointment, date=date, time="10:00", purpose="one")

    assert len(made["reference"]) == booking._REF_LENGTH
    found = await booking._find_booking(made["reference"], "")
    assert found is not None


async def test_a_reference_is_matched_however_the_model_spaces_it(_a_bot_on_a_call):  # noqa: ARG001
    """Speech-to-text renders a spelled-out code inconsistently."""
    date = _future_date()
    made = await _call(booking.book_appointment, date=date, time="10:00", purpose="one")
    spaced = " ".join(made["reference"])

    result = await _call(booking.cancel_appointment, reference=spaced.lower())
    assert result["cancelled"] is True, result


# --- wiring ---------------------------------------------------------------------

def test_the_booking_template_is_what_a_bot_gets_by_default():
    from app.pipeline.tools import TOOLS

    names = [fn.__name__ for fn in TOOLS]
    assert "book_appointment" in names
    assert "check_availability" in names
    assert "cancel_appointment" in names
    assert "reschedule_appointment" in names


def test_the_pipeline_tells_the_tools_which_call_they_are_on():
    """Without this the booking tools have no bot and default to UTC."""
    import inspect

    from app.pipeline import voice_pipeline

    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "call_context.set_call(" in source
