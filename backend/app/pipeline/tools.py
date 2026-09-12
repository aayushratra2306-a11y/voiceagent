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

from datetime import UTC, datetime

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from app.models.order import Order
from app.pipeline.booking import BOOKING_TOOLS, get_config


async def get_current_datetime(params: FunctionCallParams):
    """Get the current real-world date and time, in the caller's own time zone.

    Use this whenever the caller asks what the date or time is, or asks
    something relative to "today" or "right now" that you would otherwise
    have to guess at — you do not know the current date on your own.

    `human_readable` is already in the caller's local time zone and already
    names that zone. Say it as it is given to you. Do NOT convert it, do not
    add or subtract hours, and do not explain the offset between time zones —
    the conversion has already been done for you.
    """
    # Found on a live call, 2026-09-11: the caller asked the date and time,
    # got the date right and the time 5.5 hours behind — exactly the UTC-to-
    # IST offset — because this returned datetime.now(UTC) stamped "UTC" and
    # knew nothing about the bot's configured zone.
    #
    # On the second call the model did something more revealing still: it
    # read the UTC time out, then remarked that "IST is UTC+5:30" without
    # ever doing the arithmetic. That is the lesson this project keeps
    # relearning — the model is not a calculation layer. Handing it a value
    # in the wrong zone and hoping it converts is the same class of mistake
    # as expecting it to enforce business hours (see booking.py's
    # _within_business_hours). Convert here, in code, and give it a sentence
    # it only has to repeat.
    #
    # booking.get_config() already solves exactly this: the bot's zone, read
    # once per call and cached, with a spoken name ("India time", not
    # "Asia/Kolkata"). Reusing it is also what keeps "what time is it" and
    # "what time can I book" from disagreeing inside one conversation.
    now_utc = datetime.now(UTC)
    config = await get_config()
    local = now_utc.astimezone(config.zone)

    # Same spoken shape booking.py uses (see its _spoken): 12-hour, no
    # leading zero, minutes omitted on the hour — "8 am", not "08:00".
    hour = local.strftime("%I").lstrip("0") or "12"
    minute = "" if local.minute == 0 else f":{local.minute:02d}"
    spoken_time = f"{hour}{minute} {local.strftime('%p').lower()}"

    logger.info("[TOOL] get_current_datetime called")

    result = {
        # Deliberately still UTC: this is the machine-readable field, and
        # anything else that reads it (logs, another tool) should get an
        # unambiguous instant rather than having to guess a zone.
        "iso_datetime": now_utc.isoformat(),
        "human_readable": (
            f"{local.strftime('%A, %d %B %Y')}, {spoken_time} {config.spoken_zone}"
        ),
        "timezone": config.timezone,
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


# The set of tools a bot gets when it has none configured of its own.
#
# Task 3.5 replaced the original one-function book_appointment with the
# booking TEMPLATE — check availability, book, cancel, reschedule — which
# handles time zones, the slot being taken mid-booking, and giving the
# caller a reference code they can repeat back. The name book_appointment
# is deliberately unchanged so bots that already reference it keep working;
# what changed is everything behind it.
#
# Per-bot tool selection is task 3.1 (see services/tool_registry.py): a bot
# with its own tools configured gets exactly those, and this list is the
# fallback for every bot that has configured nothing.
TOOLS = [
    get_current_datetime,
    get_order_status,
    *BOOKING_TOOLS,
]
