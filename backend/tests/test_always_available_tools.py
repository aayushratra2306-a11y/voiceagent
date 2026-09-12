"""Live call defect, found 2026-09-12 — a configured bot had no clock.

The caller asked the bot for the date and time. It answered confidently and
wrongly, and even volunteered that "IST is UTC+5:30" — reasoning from its own
general knowledge rather than reading anything real.

One log line explained it:

    [TOOLS] Bot 6a8d5a4c...: loaded 8 configured tool(s)

and no tool call after it. load_tools_for_bot gives a bot with configured
tools exactly those tools and drops every built-in, so this bot had no
get_current_datetime to call. With no clock to read, the model invented one.

That per-bot rule is right for DOMAIN tools and stays: a tutor bot has no
business being offered book_appointment, and a shorter tool list is an easier
choice for the model to make. But knowing what day it is is not a domain
capability — it is ambient context, closer to knowing its own name than to
knowing how to issue a refund. Any bot can be asked "are you open tomorrow?",
and the alternative to answering from a clock is answering from invention.

So exactly one built-in is now offered to every bot regardless of
configuration. get_order_status and the booking template stay opt-in,
because they genuinely are domain-specific.

Worth noting what this bug cost: the timezone fix committed an hour earlier
was real and correct, and made no difference to this bot, because the
function it fixed was never reachable here. Fixing the value a tool returns
is wasted while the tool is not being offered at all.
"""

import pytest

from app.services import tool_registry

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeTool:
    """One configured BotTool row, enough of one for load_tools_for_bot."""

    def __init__(self, name: str, kind: str = "http"):
        self.name = name
        self.kind = kind
        self.builtin = ""
        self.enabled = True
        self.description = "a configured tool"
        self.long_running = False
        self.undo = None
        self.payment = None
        self.approval = None
        self.id = "tool-id"

    def json_schema(self):
        return ({}, [])


@pytest.fixture
def bot_with(monkeypatch):
    """Load tools for a bot whose configured rows are whatever you pass."""

    async def load(records):
        class _FakeBotTool:
            bot_id = "unused — the stub ignores the query"
            enabled = True

            @staticmethod
            def find(*a, **k):
                class _Result:
                    async def to_list(_self):
                        return records

                return _Result()

        monkeypatch.setattr(tool_registry, "BotTool", _FakeBotTool)
        tools, *_flags = await tool_registry.load_tools_for_bot("bot-1")
        return [getattr(t, "__name__", getattr(t, "name", "?")) for t in tools]

    return load


async def test_a_configured_bot_still_gets_a_clock(bot_with):
    """The live failure, reproduced. Before the fix this returned only the
    two configured tools and the model had nothing to ask for the time."""
    names = await bot_with([_FakeTool("lookup_repo"), _FakeTool("book_slot")])

    assert "get_current_datetime" in names, (
        "a bot with configured tools has no clock — it will invent the time"
    )


async def test_domain_builtins_are_still_opt_in(bot_with):
    """The fix must stay narrow. Re-adding every built-in would undo the
    thing the per-bot rule exists for: a shorter, more relevant tool list."""
    names = await bot_with([_FakeTool("lookup_repo")])

    assert "get_order_status" not in names
    assert "book_appointment" not in names
    assert "check_availability" not in names


async def test_a_customers_own_tool_of_that_name_wins(bot_with):
    """Two functions with one name is not expressible in the schema sent to
    the provider — it either rejects the request or silently keeps one. If a
    customer has built their own get_current_datetime, theirs is the one that
    should run."""
    names = await bot_with([_FakeTool("get_current_datetime")])

    assert names.count("get_current_datetime") == 1, "the name was offered twice"


async def test_a_bot_with_nothing_configured_is_unchanged(bot_with):
    """The existing fallback keeps working exactly as it did — this fix adds
    a floor for configured bots, it does not touch unconfigured ones."""
    names = await bot_with([])

    assert "get_current_datetime" in names
    assert "get_order_status" in names
    assert "book_appointment" in names


async def test_the_always_available_set_stays_deliberately_small():
    """A guard on scope rather than behaviour. Anything added here is added
    to every bot's prompt on every turn forever, so it should be a decision
    someone makes on purpose, not somewhere tools accumulate by default."""
    from app.pipeline.tools import ALWAYS_AVAILABLE

    names = {fn.__name__ for fn in ALWAYS_AVAILABLE}
    assert names == {"get_current_datetime"}, (
        f"ALWAYS_AVAILABLE has grown to {names} — every entry costs every bot "
        f"prompt space on every turn; add deliberately or not at all"
    )
