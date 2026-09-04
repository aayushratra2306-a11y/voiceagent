"""Task 3.1 — a tool as a database record instead of a Python function.

Until now every bot got the same three hardcoded tools, so giving one
customer a tool meant writing code and deploying. That does not scale past
a handful of customers, which is the whole point of this task: adding a
tool becomes a form someone fills in.

Two kinds of tool live here:

  builtin — names one of the functions already in the codebase. Kept
    because some tools genuinely need real logic (checking a slot is free
    before booking it), and because existing bots must keep working.

  http — describes a call to any REST API: method, URL, headers, query,
    body, and where the credential goes. This is the one that matters. The
    manual's own warning on this task is that the generic HTTP tool should
    be as capable as possible, because every API it can reach by
    configuration alone is an integration that costs nobody a deployment.

The templating is what makes that true. Anywhere in the URL, headers,
query or body, `{placeholder}` is replaced with a value the AI supplied —
so `https://api.shop.com/orders/{order_id}` with one declared parameter
covers a large share of real REST endpoints without new code.
"""

from typing import Any, Literal

from beanie import Document
from pydantic import BaseModel, Field, field_validator

# What the AI is allowed to be asked for. Deliberately the JSON Schema
# primitives and nothing more: these are values a language model produces
# from a spoken sentence, and nested objects are a reliable way to get
# malformed arguments back.
ParamType = Literal["string", "number", "integer", "boolean"]

AuthKind = Literal["none", "bearer", "header", "query", "basic"]

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


class ToolParameter(BaseModel):
    """One input the AI must work out from the conversation.

    `description` is not documentation — it is the only thing the model
    reads when deciding what to put here, so "the customer's order number,
    digits only" behaves very differently from "order id".
    """

    name: str
    type: ParamType = "string"
    description: str = ""
    required: bool = True

    @field_validator("name")
    @classmethod
    def _identifier(cls, v: str) -> str:
        # Placeholders are substituted by exact name, and the schema goes to
        # the model as JSON. A name with braces or spaces would either break
        # substitution or produce a schema the provider rejects.
        v = v.strip()
        if not v.isidentifier():
            raise ValueError("parameter name must be a plain identifier, e.g. order_id")
        return v


class ToolAuth(BaseModel):
    """How the customer's API recognises them.

    The secret is stored encrypted (see core/crypto.py) and never returned
    by the API — the tool detail endpoint sends a masked form instead.
    """

    kind: AuthKind = "none"
    # Header name for kind="header", query parameter name for kind="query".
    name: str = ""
    secret_encrypted: str = ""


class ToolUndo(BaseModel):
    """Task 3.4 — how this tool takes back what it did.

    Optional, and its absence is meaningful rather than an oversight: a
    lookup has nothing to undo, and a sent message or a charged card cannot
    be undone at all. Only a tool that declares this is ever rolled back,
    so the configuration carries the intent and the saga never guesses it.

    The undo call sees the same arguments the original did, so a cancel URL
    can be written with the same placeholders — `/bookings/{booking_id}`
    reverses `/bookings` called with that id.
    """

    url: str = ""
    method: str = "DELETE"
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)


class BotTool(Document):
    """One configured tool belonging to one bot."""

    bot_id: str
    # What the model calls it. Must be a valid identifier because it becomes
    # a function name in the schema sent to the provider.
    name: str
    # What the model reads to decide whether this tool is the right one.
    description: str
    enabled: bool = True

    # Task 3.3 — a tool the caller should not wait in silence for. It starts
    # in the background, returns an acknowledgement immediately, and its real
    # result is spoken when it arrives. Off by default: most APIs answer fast
    # enough, and the acknowledgement costs an extra conversational turn.
    long_running: bool = False

    kind: Literal["builtin", "http"] = "http"

    # kind="builtin"
    builtin: str = ""

    # kind="http"
    method: str = "GET"
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    # Sent as JSON for methods that carry a body. Values are templated the
    # same way as everything else.
    body: dict[str, Any] = Field(default_factory=dict)

    parameters: list[ToolParameter] = Field(default_factory=list)
    auth: ToolAuth = Field(default_factory=ToolAuth)
    undo: ToolUndo = Field(default_factory=ToolUndo)

    class Settings:
        name = "bot_tools"

    def as_undo_tool(self) -> "BotTool":
        """This tool's undo, shaped as a tool the HTTP caller can run.

        Reuses the same execution path — templating, authentication, error
        handling — rather than a second, subtly different one. The
        credential is carried across because a cancel endpoint needs the
        same authorisation the booking did.
        """
        return BotTool(
            bot_id=self.bot_id,
            name=f"undo_{self.name}",
            description=f"Undo {self.name}",
            kind="http",
            method=self.undo.method,
            url=self.undo.url,
            headers=self.undo.headers or self.headers,
            body=self.undo.body,
            auth=self.auth,
        )

    @field_validator("name")
    @classmethod
    def _valid_function_name(cls, v: str) -> str:
        v = v.strip()
        if not v.isidentifier():
            raise ValueError("tool name must be a plain identifier, e.g. check_stock")
        return v

    @field_validator("method")
    @classmethod
    def _known_method(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in HTTP_METHODS:
            raise ValueError(f"method must be one of {sorted(HTTP_METHODS)}")
        return v

    def json_schema(self) -> tuple[dict[str, Any], list[str]]:
        """The parameter shape the model is shown, as JSON Schema.

        Returns (properties, required) because that is the pair
        pipecat's FunctionSchema takes.
        """
        properties = {
            p.name: {"type": p.type, "description": p.description}
            for p in self.parameters
        }
        required = [p.name for p in self.parameters if p.required]
        return properties, required
