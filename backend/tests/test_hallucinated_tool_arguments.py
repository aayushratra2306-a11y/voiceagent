"""Live call defect, found 2026-09-12 — the model invented an argument.

Third and final layer of the "bot tells the wrong time" defect. The first
fix converted the time zone; the second made sure the clock was offered to
configured bots at all. With both deployed, the caller asked the time and
still did not get it. The logs said why:

    Calling function [get_current_datetime] with arguments
        {'human_readable': '2026-09-12 14:35:20 PDT'}
    ERROR ... get_current_datetime() got an unexpected keyword argument
        'human_readable'

`get_current_datetime` takes no arguments. Confirmed against pipecat's own
schema generator, not assumed: DirectFunctionWrapper builds properties from
the SIGNATURE, skipping the special first `params`, so the schema sent to
Groq advertises `properties: {}`. The model was told the tool takes nothing
and passed something anyway — it read `human_readable` out of the
docstring, which named it while explaining what comes BACK, and supplied a
plausible-looking US Pacific timestamp it had invented outright.

pipecat then does this (adapters/schemas/direct_function.py:289):

    return await self.function(params=params, **args)

with no filtering against the schema it just generated. So one hallucinated
key is a hard TypeError, the tool never runs, and the caller gets an answer
the model made up — which is the exact failure the tool exists to prevent.

Two things follow, and both are tested here.

The first is that this is not a get_current_datetime bug. Every built-in is
called through that same line, so a hallucinated argument crashes
get_order_status or book_appointment identically. A caller would hear
"I've booked that" for a booking that raised a TypeError.

The second is what NOT to do. The obvious fix — `**_ignored` on the
signature — is worse than the bug: pipecat's loop does not skip VAR_KEYWORD
parameters, so `_ignored` is advertised to the model as a REQUIRED
parameter. Verified by running it. test_the_schema_is_untouched and
test_no_tool_advertises_a_private_parameter exist to keep anyone (me
included) from "simplifying" the fix back into that hole.

The rule this encodes: arguments from a model are untrusted input, exactly
like the values inside them. tools.py has said so in its header since Phase
1 — "the AI can and occasionally will invent a plausible-looking but wrong
value" — and this is that sentence coming true one level up, about the
shape of the call rather than its contents.
"""

import pytest
from pipecat.adapters.schemas.direct_function import DirectFunctionWrapper

from app.pipeline import tools

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _Params:
    """Stands in for pipecat's FunctionCallParams. Captures the result."""

    def __init__(self):
        self.result = None
        self.arguments = {}

    async def result_callback(self, result):
        self.result = result


def _advertised(fn):
    """What pipecat actually sends the provider for this function."""
    return DirectFunctionWrapper(fn).to_function_schema()


def _clock():
    """The always-available clock, as the pipeline hands it over."""
    return next(f for f in tools.ALWAYS_AVAILABLE if f.__name__ == "get_current_datetime")


# --- the live crash -----------------------------------------------------------

async def test_the_live_crash_reproduced(monkeypatch):
    """The exact call from the 17:08:21 log line, through pipecat's own
    invoke path. Before the fix this raised TypeError and the caller was
    told a time that had been invented."""
    monkeypatch.setattr(tools, "get_config", _fake_config)
    wrapper = DirectFunctionWrapper(_clock())
    params = _Params()

    await wrapper.invoke({"human_readable": "2026-09-12 14:35:20 PDT"}, params=params)

    assert params.result is not None, "the tool never ran"
    assert "India" in params.result["human_readable"], (
        "the tool ran but did not answer in the caller's zone"
    )


async def test_every_built_in_survives_an_invented_argument():
    """Not a clock bug — every built-in goes through the same splat. A
    booking that raises TypeError is worse than a time that does: the model
    is told the call failed, but the caller may already have been told it
    worked."""
    for fn in tools.TOOLS:
        wrapper = DirectFunctionWrapper(fn)
        try:
            await wrapper.invoke({"totally_made_up": "x"}, params=_Params())
        except TypeError as e:
            if "unexpected keyword argument" in str(e):
                pytest.fail(f"{fn.__name__} still crashes on an invented argument: {e}")
        except Exception:
            # Anything else is the tool's own business — a missing required
            # argument, no database in this test. Only the crash matters here.
            pass


# --- the trap -----------------------------------------------------------------

async def test_the_schema_is_untouched():
    """The fix must be invisible to the model. If tolerating junk changes
    what is advertised, the fix has caused a worse bug than it solved."""
    schema = _advertised(_clock())

    assert schema.name == "get_current_datetime"
    assert schema.properties == {}, (
        f"the clock now advertises parameters it does not take: {schema.properties}"
    )
    assert schema.required == []


async def test_no_tool_advertises_a_private_parameter():
    """The specific hole `**_ignored` would open. pipecat does not skip
    VAR_KEYWORD parameters, so a catch-all in the signature is published to
    the model as a required argument — verified by running it, which is the
    only reason this test exists."""
    for fn in [*tools.TOOLS, *tools.ALWAYS_AVAILABLE]:
        schema = _advertised(fn)
        for name in schema.properties:
            assert not name.startswith("_"), (
                f"{fn.__name__} advertises a private parameter {name!r} — a "
                f"catch-all leaked into the schema"
            )
            assert name not in ("kwargs", "args"), (
                f"{fn.__name__} advertises {name!r} to the model"
            )


# --- it must still do its job -------------------------------------------------

async def test_real_arguments_still_arrive():
    """Filtering unknown keys must not filter known ones. get_order_status
    without its order_id would look fixed and be useless."""
    schema = _advertised(next(f for f in tools.TOOLS if f.__name__ == "get_order_status"))

    assert "order_id" in schema.properties
    assert schema.required == ["order_id"]

    seen = {}

    async def fake_find_one(*a, **k):
        seen["called"] = True
        return None

    from app.models.order import Order

    original = Order.find_one
    Order.find_one = fake_find_one
    try:
        params = _Params()
        await DirectFunctionWrapper(
            next(f for f in tools.TOOLS if f.__name__ == "get_order_status")
        ).invoke({"order_id": "ORD-1001", "invented": "junk"}, params=params)
    finally:
        Order.find_one = original

    assert seen.get("called"), "the real argument was dropped along with the invented one"
    assert params.result["found"] is False


async def test_a_dropped_argument_is_logged():
    """Swallowing this silently would hide a model misbehaving. This bug
    took three deploys to find precisely because the evidence kept not
    being in the logs."""
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING")
    try:
        await DirectFunctionWrapper(_clock()).invoke(
            {"human_readable": "nonsense"}, params=_Params()
        )
    finally:
        logger.remove(sink)

    assert any("human_readable" in line for line in lines), (
        "an invented argument was dropped without saying so"
    )


# --- the trigger --------------------------------------------------------------

async def test_the_clock_does_not_name_a_return_field_in_its_description():
    """Why the model invented THIS argument rather than any other: the
    description named `human_readable` while explaining what comes back,
    and a tool with an empty parameter list plus a named field in its prose
    is an invitation. The description is the only thing the model reads
    about a tool, so it should describe what the tool does, never the shape
    of its return value."""
    description = _advertised(_clock()).description

    assert "human_readable" not in description, (
        "the description still names the field the model tried to supply"
    )


async def _fake_config(*a, **k):
    class _C:
        from zoneinfo import ZoneInfo

        timezone = "Asia/Kolkata"
        zone = ZoneInfo("Asia/Kolkata")
        spoken_zone = "India time"

    return _C()
