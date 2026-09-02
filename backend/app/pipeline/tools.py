"""Task 1.3 — the function-calling framework.

This gives the bot "hands": instead of only being able to talk about what's in
its instructions and uploaded documents, it can now ask to run a real Python
function and use the result in its reply.

How it works (pipecat 1.7.0's actual mechanism, verified against the
installed source — not guessed):

  1. Write a normal async function. Its FIRST parameter must be named
     `params` (type `FunctionCallParams` — pipecat detects this by name).
     Every other parameter is an argument the AI can fill in, with its type
     hint controlling the JSON schema pipecat generates automatically.
  2. Write a proper docstring — the AI reads the function's docstring and
     each argument's `Args:` description to decide *when* and *how* to call
     it. This is the only "prompt engineering" tool definitions need; there
     is no separate schema to hand-write.
  3. Call `await params.result_callback(result)` with a small dict, never a
     sentence — the AI turns structured data into natural spoken language
     itself far better than it can be told to.
  4. List the function in TOOLS below. That's the entire registration step —
     pipecat wires it into the LLM request and dispatches calls back to it
     automatically; no manual `register_function` call needed.

Every handler validates its own inputs and treats what the AI provides as
untrusted — the AI can and occasionally will invent a plausible-looking but
wrong value.
"""

import re
from datetime import UTC, datetime

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from app.models.appointment import Appointment
from app.models.order import Order


async def get_current_datetime(params: FunctionCallParams):
    """Get the current real-world date and time.

    Use this whenever the caller asks what the date or time is, or asks
    something relative to "today" or "right now" that you would otherwise
    have to guess at — you do not know the current date on your own.
    """
    now = datetime.now(UTC)
    logger.info("[TOOL] get_current_datetime called")

    result = {
        "iso_datetime": now.isoformat(),
        "human_readable": now.strftime("%A, %d %B %Y, %H:%M UTC"),
    }
    logger.info(f"[TOOL] get_current_datetime -> {result['human_readable']}")
    await params.result_callback(result)


async def get_order_status(params: FunctionCallParams, order_id: str):
    """Look up the current status and delivery date of a customer's order.

    Use this whenever the caller asks about an order, a delivery, or where
    something they bought has got to. Do not guess a status or delivery date
    yourself — always call this and use exactly what it returns.

    Args:
        order_id: The order ID the caller gave you, e.g. "ORD-1001". If they
            just say a number, still pass it through as given — don't
            reformat or guess at the correct prefix yourself.
    """
    logger.info(f"[TOOL] get_order_status called with order_id={order_id!r}")

    # Deliberately untrusted: the AI can hand back a garbled or invented ID
    # (mis-heard speech, a hallucinated format) — normalize lightly, but
    # never assume it's well-formed.
    normalized = order_id.strip().upper()
    order = await Order.find_one(Order.order_id == normalized)

    if not order:
        logger.warning(f"[TOOL] get_order_status: no order found for {normalized!r}")
        # Structured, honest failure — never invent a status/date for an
        # order that doesn't exist. This is the exact case the manual's
        # task 1.4 tip warns about.
        await params.result_callback({
            "found": False,
            "message": f"No order found with ID {normalized}. Ask the caller to double check the order ID.",
        })
        return

    result = {
        "found": True,
        "order_id": order.order_id,
        "item_name": order.item_name,
        "status": order.status,
        "delivery_date": order.delivery_date,
    }
    logger.info(f"[TOOL] get_order_status -> {result}")
    await params.result_callback(result)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


async def book_appointment(params: FunctionCallParams, date: str, time: str, purpose: str):
    """Book an appointment slot for the caller.

    Always confirm the date, time, and purpose back to the caller in your
    reply before or after calling this — never book silently. If this
    reports the slot is already taken, tell the caller plainly and ask for
    a different time; do not just say it's booked anyway.

    Args:
        date: The appointment date in YYYY-MM-DD format (e.g. "2026-09-03").
            Work out the correct date yourself from what the caller said
            (e.g. "tomorrow", "next Monday") using the current date from
            get_current_datetime if you need it — do not pass relative
            phrases like "tomorrow" through directly.
        time: The appointment time in 24-hour HH:MM format (e.g. "15:00" for
            3pm). Convert from whatever format the caller used.
        purpose: A short description of what the appointment is for.
    """
    logger.info(f"[TOOL] book_appointment called with date={date!r} time={time!r} purpose={purpose!r}")

    # Validate before touching the database — the AI is not guaranteed to
    # follow the requested format even when told to.
    if not _DATE_RE.match(date) or not _TIME_RE.match(time):
        logger.warning(f"[TOOL] book_appointment: bad format date={date!r} time={time!r}")
        await params.result_callback({
            "booked": False,
            "message": (
                "The date or time wasn't in a valid format. Work out the exact "
                "date as YYYY-MM-DD and the time as 24-hour HH:MM, then try again."
            ),
        })
        return

    existing = await Appointment.find_one(Appointment.date == date, Appointment.time == time)
    if existing:
        logger.info(f"[TOOL] book_appointment: slot {date} {time} already taken")
        await params.result_callback({
            "booked": False,
            "message": f"The {time} slot on {date} is already booked. Ask the caller for a different time.",
        })
        return

    appointment = Appointment(date=date, time=time, purpose=purpose)
    await appointment.insert()

    result = {"booked": True, "date": date, "time": time, "purpose": purpose}
    logger.info(f"[TOOL] book_appointment -> {result}")
    await params.result_callback(result)


# The set of tools every bot currently has access to. Per-bot, DB-configured
# tool selection is later work (see the master plan's Phase 3, task 3.1) —
# for now, every bot gets the same small, fixed set.
TOOLS = [
    get_current_datetime,
    get_order_status,
    book_appointment,
]
