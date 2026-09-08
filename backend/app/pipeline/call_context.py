"""Which call this process is currently running.

The built-in tools are plain module-level functions — that is what lets
pipecat read their type hints and docstrings automatically (task 1.3). It
also means they receive no bot, no session and no pipeline task, only the
arguments the model supplied. The booking template needs the bot's time
zone, and the payment template needs the bot and session to attach a
payment to; neither can be a function argument, because the model must not
be able to choose them.

So they read it from here, and here is a module-level global. That is
normally the wrong shape, and it is the right one in this codebase for a
specific architectural reason:

    every call runs in its own OS process.

Task 2.4 spawns one worker process per call (spawn, never fork — see
call_worker.py), so a module-level value in that process describes exactly
one call for that process's entire life. There is no second call to leak
into. Should that ever stop being true, this module is the single place
that has to change, which is most of why it exists as its own file rather
than as a global in booking.py.

The API process imports this too — the webhook route reads nothing from it,
but tool_registry and the models are shared — so `current()` returning an
empty context is a normal, supported state rather than an error.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CallContext:
    """What the tools in this process are allowed to know about the call."""

    bot_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    # Task 3.7 — the same id app.api.connect keys its live-call registry by
    # (the dict key `_active_calls` uses, not session_id above — a separate
    # id decided by the WebRTC handler before the pipeline even starts).
    # A payment tool stamps this onto the PaymentSession it creates, so a
    # provider's webhook arriving later can find this exact call and speak
    # into it while it is still live.
    pc_id: str | None = None
    language: str = "en"
    # Task 3.3's background runner, so a builtin tool can also hand slow work
    # off instead of holding the turn. None outside a call.
    jobs: Any = None
    # Cached per-process lookups (the bot's booking settings, for instance),
    # so a tool called five times in one call does not query five times.
    cache: dict[str, Any] = field(default_factory=dict)


_current = CallContext()


def set_call(**fields: Any) -> CallContext:
    """Called once, by the pipeline, as a call starts."""
    global _current
    _current = CallContext(**fields)
    return _current


def current() -> CallContext:
    """The call this process is running, or an empty context outside one."""
    return _current


def clear() -> None:
    """Only used by tests; a real worker process exits instead."""
    global _current
    _current = CallContext()
