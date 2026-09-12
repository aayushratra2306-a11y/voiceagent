"""Live call defect, found 2026-09-11 — get_current_datetime answered in UTC.

The caller asked "what's today's date and time?". The date came back right
(UTC and IST only disagree on the date near midnight IST). The time came
back exactly 5.5 hours behind — precisely the UTC-to-IST offset — because
get_current_datetime() did:

    now = datetime.now(UTC)
    result = {"human_readable": now.strftime("%A, %d %B %Y, %H:%M UTC")}

with no awareness of the bot's configured time zone at all.

The second call was more revealing: the bot read the UTC time out loud and
then remarked that "IST is UTC+5:30" — without ever doing the arithmetic.
That is this project's recurring lesson in another costume: the model is not
a calculation layer, the same way it is not a validation layer (see
booking.py's _within_business_hours, added after a caller booked outside
business hours). Hand it a value in the wrong zone and hope it converts, and
it will happily narrate the offset instead of applying it.

booking.get_config() already solved this: the bot's zone, read once per call
and cached, with a spoken name ("India time", not "Asia/Kolkata"). This tool
simply never used it. Reusing it is also what stops "what time is it" and
"what time can I book" disagreeing inside one conversation.

These tests stub get_config rather than inserting a Bot, deliberately: what
is under test is the conversion and the wording, not Beanie's ability to
read a document back. booking's own tests already cover the lookup.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.pipeline import booking, tools
from app.pipeline.tools import get_current_datetime

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _Params:
    """Stands in for pipecat's FunctionCallParams."""

    def __init__(self):
        self.result = None

    async def result_callback(self, result):
        self.result = result


@pytest.fixture
def bot_in_zone(monkeypatch):
    """Pretend the bot on this call is configured with the given zone."""
    def configure(timezone: str):
        async def fake_config():
            return booking.BookingConfig(timezone=timezone)

        monkeypatch.setattr(tools, "get_config", fake_config)

    return configure


async def _call_it():
    params = _Params()
    await get_current_datetime(params)
    return params.result


async def test_the_time_is_in_the_bots_own_zone_not_utc(bot_in_zone):
    """The bug, reproduced. IST is UTC+5:30, so a naive UTC read is 5.5
    hours behind the clock the caller is actually looking at."""
    bot_in_zone("Asia/Kolkata")

    result = await _call_it()

    local = datetime.now(UTC).astimezone(ZoneInfo("Asia/Kolkata"))
    expected_hour = local.strftime("%I").lstrip("0") or "12"
    assert f"{expected_hour}:{local.minute:02d}" in result["human_readable"] or (
        local.minute == 0 and f"{expected_hour} " in result["human_readable"]
    ), f"expected the India-time hour in {result['human_readable']!r}"


async def test_the_zone_is_named_the_way_booking_speaks_it(bot_in_zone):
    """Consistency with the booking tools: both should say "India time" out
    loud rather than reading the IANA name or leaving it unsaid."""
    bot_in_zone("Asia/Kolkata")

    result = await _call_it()

    assert "India time" in result["human_readable"]
    assert "UTC" not in result["human_readable"], (
        "still labelled UTC — the caller hears a UTC time presented as their own"
    )


async def test_a_different_bot_zone_is_respected(bot_in_zone):
    """Not hardcoded to one zone — whatever the bot is configured with."""
    bot_in_zone("America/New_York")

    result = await _call_it()

    assert "US Eastern time" in result["human_readable"]
    assert "India time" not in result["human_readable"]


async def test_a_utc_bot_still_says_utc(bot_in_zone):
    """A bot genuinely configured for UTC should say so — the fix removes a
    wrong label, not the ability to name UTC when it is correct."""
    bot_in_zone("UTC")

    result = await _call_it()

    assert "UTC" in result["human_readable"]


async def test_the_spoken_time_is_twelve_hour(bot_in_zone):
    """Matches booking.py's _spoken: "8 am", not "08:00". A phone call is
    the one place 24-hour clock reads worst."""
    bot_in_zone("Asia/Kolkata")

    result = await _call_it()

    said = result["human_readable"]
    assert ("am" in said) or ("pm" in said), f"no am/pm in {said!r}"


async def test_the_iso_datetime_field_stays_in_utc(bot_in_zone):
    """iso_datetime is the machine-readable field — logs and any other tool
    reading it should get an unambiguous instant. Only the spoken form
    follows the bot's zone."""
    bot_in_zone("Asia/Kolkata")

    result = await _call_it()

    parsed = datetime.fromisoformat(result["iso_datetime"])
    assert parsed.utcoffset().total_seconds() == 0, "iso_datetime drifted off UTC"


async def test_the_timezone_is_reported_for_the_model(bot_in_zone):
    """The IANA name rides along so the model (and the transcript) can tell
    which zone was applied, without it being the thing spoken aloud."""
    bot_in_zone("Asia/Kolkata")

    result = await _call_it()

    assert result["timezone"] == "Asia/Kolkata"


async def test_the_docstring_tells_the_model_not_to_convert():
    """The model narrated the offset instead of applying it ("IST is
    UTC+5:30"). The conversion is done in code now, and the docstring — the
    only instruction the model gets about this tool — has to say so, or the
    same helpfulness will reintroduce the same wrong answer."""
    # Whitespace-normalised, because the instruction is prose in a wrapped
    # docstring: a line break landing between "do not" and "convert" is a
    # reformat, not a behaviour change, and this test failing for that
    # reason teaches nothing. It should fail only if the instruction goes.
    doc = " ".join((get_current_datetime.__doc__ or "").split()).lower()

    assert "do not convert" in doc
