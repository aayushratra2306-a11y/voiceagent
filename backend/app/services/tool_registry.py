"""Task 3.1 — turning tool records into tools a call can actually use.

`load_tools_for_bot()` is called once when a call starts. It reads that
bot's configured tools and hands back a list pipecat accepts directly:
plain functions for the built-in ones, and `FunctionSchema` objects with a
generated handler for the HTTP ones.

That FunctionSchema takes a `handler` is what makes this task possible at
all. Pipecat's usual route reads a real Python function's type hints and
docstring, which a database record does not have — but a schema built at
runtime with a closure attached is treated identically by the LLM service.

The generic HTTP tool is the part worth reading. The manual's warning on
this task is that its capability decides whether a customer integration is
a form or a development project, so it supports:

  - any of GET/POST/PUT/PATCH/DELETE
  - `{placeholder}` substitution in the URL, headers, query and body, from
    the arguments the model supplied
  - bearer, custom-header, query-parameter and basic authentication
  - a JSON body, and a JSON or text response

which between them cover most REST APIs a small business actually has.

Task 3.6 — "where is my order" is the single most common support question
there is, and a lookup tool built once has to work against APIs shaped
however the customer's happens to be shaped. Three things on top of the
above make that true rather than aspirational:

  - `field_map` normalizes an arbitrary response into names the model can
    rely on regardless of how deeply the real API nests them — see
    `_resolve_path`.
  - a per-tool timeout, because the manual is specific that a lookup needs
    a much shorter fuse than a booking does: "around three seconds... far
    better to say their system is not responding than to leave the caller
    in silence."
  - a call-scoped cache, so asking the same question twice in one call (a
    caller circling back, or the model double-checking) does not repeat the
    network request — see the note on call_context above `_cache_key`.
"""

import asyncio
import base64
import json
from typing import Any

import httpx
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema

from app.core.crypto import decrypt_secret
from app.models.bot_tool import BotTool
from app.pipeline import call_context

# A customer's API is on the far side of a live phone call. Past a few
# seconds the caller is listening to silence, so a slow API has to become a
# spoken "that is taking a while" rather than an unbounded wait. Task 3.3
# moves genuinely slow ones to the background; this is the ceiling for
# everything that stays in the foreground, and BotTool.timeout_seconds lets
# one tool set a tighter ceiling than this default (task 3.6's lookup
# template is meant to use one).
HTTP_TIMEOUT_SECONDS = 8.0

# Enough for the model to act on, small enough not to flood the prompt. A
# customer API returning a large document would otherwise push the actual
# conversation out of the context window.
MAX_RESPONSE_CHARS = 4000


def _render(template: str, args: dict[str, Any]) -> str:
    """Substitute {placeholders} with the model's arguments.

    Deliberately not str.format: a customer's URL or JSON body may contain
    braces of its own, and format() would raise or misread them. This
    replaces only the exact placeholders that were declared as parameters
    and leaves every other brace alone.
    """
    out = template
    for key, value in args.items():
        out = out.replace("{" + key + "}", "" if value is None else str(value))
    return out


def _render_map(mapping: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    rendered: dict[str, Any] = {}
    for key, value in mapping.items():
        if isinstance(value, str):
            rendered[key] = _render(value, args)
        elif isinstance(value, dict):
            rendered[key] = _render_map(value, args)
        else:
            rendered[key] = value
    return rendered


def _resolve_path(data: Any, path: str) -> Any:
    """Walk a dotted path ("data.order.status") into a parsed JSON response.

    Task 3.6's whole point: a customer's API nests things however it nests
    things, and the model should never have to know that this one buries
    status three levels deep while another puts it at the top. Missing at
    any step returns None rather than raising — a field that isn't there
    for THIS record (no tracking number yet) is a normal, common case, not
    a bug in the mapping.

    Dict traversal only, deliberately: a list index in the path (order.0.id)
    would cover a real but rarer shape, and the failure mode without it is
    legible (that one field comes back None) rather than silent.
    """
    current = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _apply_field_map(tool: BotTool, payload: Any) -> dict[str, Any] | None:
    """The AI-facing view of a response, per this tool's field_map.

    None (not {}) when no mapping is configured, so call_http_tool can tell
    "this tool doesn't use field mapping" apart from "every mapped field
    happened to resolve to nothing" — the two need different handling.
    """
    if not tool.field_map:
        return None
    return {name: _resolve_path(payload, path) for name, path in tool.field_map.items()}


def _cache_key(tool: BotTool, args: dict[str, Any]) -> str:
    """Identifies one lookup, for the call-scoped cache below."""
    return f"{tool.id or tool.name}:{sorted(args.items())}"


def _apply_auth(tool: BotTool, headers: dict[str, str], params: dict[str, Any]) -> None:
    """Add the customer's credential wherever their API expects it.

    Mutates the dicts in place. Decryption failure yields "" and is left to
    fail as an unauthorised call against their API, which is a legible
    error, rather than raising here into something unrelated.
    """
    auth = tool.auth
    if auth.kind == "none":
        return
    secret = decrypt_secret(auth.secret_encrypted)
    if not secret:
        logger.warning(f"[TOOL] {tool.name}: credential missing or unreadable")
        return

    if auth.kind == "bearer":
        headers["Authorization"] = f"Bearer {secret}"
    elif auth.kind == "header":
        headers[auth.name or "Authorization"] = secret
    elif auth.kind == "query":
        params[auth.name or "api_key"] = secret
    elif auth.kind == "basic":
        # The stored secret is "user:password"; the name field is unused.
        headers["Authorization"] = "Basic " + base64.b64encode(secret.encode()).decode()


async def call_http_tool(tool: BotTool, args: dict[str, Any]) -> dict[str, Any]:
    """Perform one configured HTTP call and return a result for the model.

    Never raises. Every failure becomes a result the model can talk about,
    because an exception here would surface to the caller as dead air or a
    dropped turn, and "I could not reach the order system" is a far better
    outcome than silence.

    Task 3.6 — a cache check opens this function for GET requests. Reusing
    the same answer to the same question asked twice in one call is safe
    only when the request has no side effect; a POST/PUT/PATCH/DELETE is
    never cached; a customer's booking or payment tool re-running because
    its arguments happened to match an earlier call would be a serious
    bug, not an optimisation. The cache itself is per-call — see
    call_context.py — so nothing here can leak between two different calls,
    let alone two different callers, even on a reused worker process.
    """
    method = tool.method.upper()
    cache_key = _cache_key(tool, args) if method == "GET" else None
    if cache_key is not None:
        ctx_cache = call_context.current().cache
        cached = ctx_cache.get(cache_key)
        if cached is not None:
            logger.info(f"[TOOL] {tool.name}: served from this call's own cache, not asked again")
            return cached

    # .strip(): BotTool.url is trimmed on save (see models/bot_tool.py) so a
    # pasted-in leading space can't reach here for anything saved from now
    # on — but a tool saved before that validator existed still has the raw
    # value on disk, and a URL httpx reads as not starting with "https://"
    # fails instantly with UnsupportedProtocol rather than ever reaching the
    # customer's API. Confirmed live 2026-09-05. Stripped here too so an
    # already-saved tool doesn't need re-editing to pick up the fix.
    url = _render(tool.url.strip(), args)
    headers = {k: str(v) for k, v in _render_map(tool.headers, args).items()}
    params = _render_map(tool.query, args)
    body = _render_map(tool.body, args) if tool.body else None
    _apply_auth(tool, headers, params)

    # Task 3.6: a lookup needs a much shorter fuse than the general default
    # — the manual's own number is "around three seconds... far better to
    # say their system is not responding than to leave the caller in
    # silence." BotTool.timeout_seconds defaults to the old global constant,
    # so nothing already configured changes behaviour; a lookup tool is the
    # one meant to be dialled down.
    timeout = tool.timeout_seconds or HTTP_TIMEOUT_SECONDS

    logger.info(f"[TOOL] {tool.name} -> {tool.method} {url}")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                tool.method, url, headers=headers, params=params,
                json=body if body else None,
            )
    except httpx.TimeoutException:
        logger.warning(f"[TOOL] {tool.name}: timed out after {timeout}s")
        return {
            "ok": False,
            "error": "timeout",
            "message": "That system did not respond in time. Tell the caller and offer to try again.",
        }
    except Exception as e:
        logger.warning(f"[TOOL] {tool.name}: {type(e).__name__}: {e}")
        return {
            "ok": False,
            "error": "unreachable",
            "message": "That system could not be reached. Tell the caller plainly rather than guessing an answer.",
        }

    text = response.text[:MAX_RESPONSE_CHARS]
    try:
        payload: Any = json.loads(text) if text.strip() else None
    except ValueError:
        payload = text

    if response.status_code >= 400:
        logger.warning(f"[TOOL] {tool.name}: HTTP {response.status_code}")
        result = {
            "ok": False,
            "error": f"http_{response.status_code}",
            "status": response.status_code,
            "data": payload,
            # 4xx is usually the caller's input (unknown order number); 5xx is
            # the customer's system. The model should say different things.
            "message": (
                "That request was rejected — the details given may be wrong. Ask the caller to confirm them."
                if response.status_code < 500
                else "That system reported an error. Tell the caller it is unavailable right now."
            ),
        }
        # Not cached: a 404 today (order not placed yet) can easily become a
        # 200 a minute from now, and this call is short enough that saving
        # one retry isn't worth risking a stale "not found".
        return result

    fields = _apply_field_map(tool, payload)
    if fields is not None and not any(v is not None for v in fields.values()):
        # Task 3.6's other not-found case: some APIs answer "no such record"
        # with a 200 and an empty body rather than a 404 — status-code
        # handling above can't see that, only the mapping can. Every field
        # this tool asked for came back missing, so this is that case.
        logger.info(f"[TOOL] {tool.name}: 200 but every mapped field was empty — treating as not found")
        result = {
            "ok": True,
            "found": False,
            "status": response.status_code,
            "message": "Nothing was found for that. Tell the caller plainly and ask them to confirm the details.",
        }
    else:
        result = {"ok": True, "status": response.status_code, "data": payload}
        if fields is not None:
            result["fields"] = fields

    if cache_key is not None:
        call_context.current().cache[cache_key] = result
    return result


def _http_handler(tool: BotTool, jobs=None, saga=None):
    """Build the function pipecat will call for this tool record.

    A closure over the record, so every configured tool gets its own
    handler without any of them existing as code.

    Task 3.3: a tool marked long-running does not block the turn. It hands
    the work to `jobs` and returns an acknowledgement straight away, so the
    caller hears "I'm looking that up" instead of ten seconds of silence.
    The real answer is spoken when it arrives.
    """

    async def handler(params) -> None:
        args = dict(params.arguments or {})

        if tool.long_running and jobs is not None:
            jobs.start(tool.name, call_http_tool(tool, args), args)
            await params.result_callback({
                "ok": True,
                "started": True,
                "message": (
                    "This is running now and will take a few moments. Tell the caller "
                    "you are working on it and carry on — do not wait, and do not say "
                    "it is done."
                ),
            })
            return

        result = await call_http_tool(tool, args)
        # Task 3.4 — the saga records what happened as it happens, so a
        # later failure in the same turn can walk the successes back.
        if saga is not None:
            await saga.record(tool.name, tool, args, result)
        await params.result_callback(result)

    return handler


def _builtin_tools() -> dict[str, Any]:
    """The functions that are still real code.

    Imported lazily: this module is reached from the API process too, and
    app.pipeline.tools pulls in pipecat.
    """
    from app.pipeline.tools import TOOLS

    return {fn.__name__: fn for fn in TOOLS}


def to_function_schema(tool: BotTool, jobs=None, saga=None) -> FunctionSchema:
    """One HTTP tool record as something the model can be offered."""
    properties, required = tool.json_schema()
    return FunctionSchema(
        name=tool.name,
        description=tool.description,
        properties=properties,
        required=required,
        handler=_http_handler(tool, jobs, saga),
    )


async def load_tools_for_bot(
    bot_id: str | None, jobs=None, saga=None
) -> tuple[list[Any], bool, bool]:
    """Every enabled tool this bot has, ready to hand to the LLM context.

    Returns (tools, has_background, has_undo). The two flags decide which
    prompt rules this bot needs — background acknowledgements (3.3) and
    partial rollback (3.4) — so a bot without such tools does not carry an
    explanation of them on every turn.

    Falls back to the built-in set when a bot has configured nothing, so
    every bot that existed before this task keeps the tools it had. A bot
    with tools configured gets exactly those — which is what makes the
    prompt smaller and the model's choice easier, rather than every bot
    carrying every tool ever written.
    """
    builtins = _builtin_tools()
    if not bot_id:
        return list(builtins.values()), False, False

    try:
        records = await BotTool.find(BotTool.bot_id == bot_id, BotTool.enabled == True).to_list()  # noqa: E712
    except Exception as e:
        # A tool-loading failure must not take the call down: the caller can
        # still have a conversation, just without tools.
        logger.warning(f"[TOOLS] Could not load tools for bot {bot_id}: {e}")
        return list(builtins.values()), False, False

    if not records:
        logger.info(f"[TOOLS] Bot {bot_id} has none configured — using the {len(builtins)} built-ins")
        return list(builtins.values()), False, False

    loaded: list[Any] = []
    for record in records:
        if record.kind == "builtin":
            fn = builtins.get(record.builtin)
            if fn is None:
                logger.warning(f"[TOOLS] {record.name}: no built-in named {record.builtin!r}")
                continue
            loaded.append(fn)
        else:
            loaded.append(to_function_schema(record, jobs, saga))

    has_background = any(r.long_running and r.kind == "http" for r in records)
    has_undo = any(r.kind == "http" and r.undo and r.undo.url for r in records)
    logger.info(
        f"[TOOLS] Bot {bot_id}: loaded {len(loaded)} configured tool(s)"
        + (" (one or more run in the background)" if has_background else "")
    )
    return loaded, has_background, has_undo


async def test_tool(tool: BotTool, args: dict[str, Any]) -> dict[str, Any]:
    """Run a tool once, outside a call, so it can be verified from the UI.

    The manual asks for this explicitly, and the reason is practical: the
    alternative way to find out whether a URL and an API key are right is
    to place a phone call and listen to the bot fail.
    """
    if tool.kind == "builtin":
        return {
            "ok": True,
            "note": f"{tool.builtin} is built in and runs in the call process; nothing to reach.",
        }
    started = asyncio.get_event_loop().time()
    result = await call_http_tool(tool, args)
    result["elapsed_ms"] = int((asyncio.get_event_loop().time() - started) * 1000)
    return result
