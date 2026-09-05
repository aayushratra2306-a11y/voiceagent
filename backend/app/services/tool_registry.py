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
from urllib.parse import quote

import httpx
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema

from app.core.crypto import decrypt_secret
from app.core.url_safety import rejection_reason
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

# Task 3.7. Appended to the system prompt only for a bot with a
# payment-enabled tool, so a bot without one carries no such instruction as
# prompt noise. The manual's tip is unambiguous: never take card numbers by
# voice, always send a link — the compliance burden of handling card data
# directly is enormous and completely avoidable by doing it this way.
PAYMENT_SAFETY_RULE = (
    "\n\nNEVER ask the caller to say, type, or otherwise give you their card "
    "number, CVV, or expiry date over this call, and never repeat one back if "
    "they offer it unprompted. If a caller starts to give you card details, stop "
    "them immediately, explain you cannot take payment details this way, and "
    "direct them to the secure payment link instead. Payment is completed only "
    "through that link — never tell the caller a payment has succeeded unless "
    "you have been explicitly told it was confirmed."
)

# Task 3.10. Appended only for a bot with an approval-gated tool. The
# manual's own reasoning: no company will let an AI approve a large refund
# unsupervised, and this is what makes them comfortable letting it handle
# everything below the threshold on its own.
APPROVAL_RULE = (
    "\n\nSome of your tools have a value above which they require a person's "
    "sign-off before they actually happen. When a tool reports that something "
    "needs approval, tell the caller plainly that this needs to be checked by a "
    "person and they will hear back separately — do not make them wait on this "
    "call for it, and never say the action is done until you are told it was "
    "approved. If it is later denied, say so honestly rather than pretending it "
    "went through."
)


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


def _render_url(template: str, args: dict[str, Any]) -> str:
    """Substitute into a URL, percent-encoding what gets substituted.

    Every other rendering target is structured — a header value, a query
    dict httpx encodes itself, a JSON body — so a stray character in a
    value stays inside that value. A URL is the exception: it is one flat
    string in which `?`, `#` and `&` are grammar, so substituting raw text
    into it lets an argument change the shape of the request rather than
    just its content. These arguments come from a language model reading
    aloud what an anonymous caller said, which is as untrusted as input
    gets — an order number of `1?admin=true` would otherwise append a
    query parameter to a customer's API call that the customer never
    configured.

    `safe="/"` rather than `safe=""`: a value that legitimately spans path
    segments (`orders/2026/17`) is a real, if uncommon, configuration, and
    encoding its slashes would break a tool that works today. Every
    character that actually carries URL grammar is still encoded.

    Traversal is handled separately below, because `/` staying safe means
    `..` survives encoding.
    """
    out = template
    for key, value in args.items():
        text = "" if value is None else str(value)
        out = out.replace("{" + key + "}", quote(text, safe="/"))
    return out


def _has_traversal(url: str) -> bool:
    """Whether a rendered URL walks up out of the path it was configured for.

    `https://api.example.com/orders/{id}` with an id of `../../admin/users`
    is a different endpoint than the one the customer configured, on the
    same host and carrying the same credential. Checked on the rendered
    path only — a `..` inside a query value is just text.
    """
    from urllib.parse import urlparse

    path = urlparse(url).path
    return ".." in path.split("/")


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


def _inside_a_call() -> bool:
    """Whether this process is actually running a call right now.

    The cache below is call-scoped, and that scoping is enforced entirely
    by the fact that a call gets its own OS process which then exits (see
    call_context.py). Outside a call — the "test this tool" button in the
    dashboard, and task 3.10's approve endpoint, both of which run
    call_http_tool from the long-lived API process — there is no such
    boundary: `call_context.current()` is a module-level object that
    process holds for its entire lifetime. Caching into it there would
    mean a Test button that shows yesterday's answer after the customer
    fixed their API, and a dict that grows for as long as the server runs.

    So the cache is switched off unless a call actually set the context.
    """
    ctx = call_context.current()
    return bool(ctx.pc_id or ctx.session_id or ctx.bot_id)


def _capped(payload: Any) -> Any:
    """The response as the model sees it, bounded in size.

    Bounding happens HERE, on the parsed value, and never on the text
    before it is parsed. Truncating a 5KB JSON document to 4000 characters
    and then parsing it yields invalid JSON, which used to fall through to
    "treat the response as plain text" — at which point every field_map
    path resolved to None and the tool reported "nothing was found" for a
    record that was returned perfectly well. A large order record is a
    completely ordinary thing for a customer's API to return, so that was
    a lookup tool confidently telling callers their order did not exist.
    """
    if payload is None or isinstance(payload, (int, float, bool)):
        return payload
    try:
        as_text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    except (TypeError, ValueError):
        as_text = str(payload)
    if len(as_text) <= MAX_RESPONSE_CHARS:
        return payload
    return {
        "truncated": True,
        "note": (
            "This response was too large to include in full. The fields this "
            "tool is configured to read were still taken from the complete "
            "response and are accurate."
        ),
        "preview": as_text[:MAX_RESPONSE_CHARS],
    }


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
    # `not tool.payment.enabled`: creating a payment link is a side effect
    # by definition — replaying a cached response would hand the caller a
    # link (and reference) from an earlier request instead of a fresh one.
    # GET already excludes it in practice (a create-link call is virtually
    # always a POST), but this is the actual invariant, stated explicitly.
    # `_inside_a_call()`: see its docstring — the cache's whole safety
    # argument is the per-call process boundary, which does not exist in
    # the API process.
    cache_key = (
        _cache_key(tool, args)
        if method == "GET" and not tool.payment.enabled and _inside_a_call()
        else None
    )
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
    url = _render_url(tool.url.strip(), args)
    if _has_traversal(url):
        logger.warning(f"[TOOL] {tool.name}: refused a rendered URL that walks out of its path: {url}")
        return {
            "ok": False,
            "error": "bad_request",
            "message": "That request could not be made with the details given. Ask the caller to confirm them.",
        }

    # A tool URL is customer-configured, and this server is the one that
    # dials it — see app/core/url_safety.py for why "the customer typed it"
    # does not make an internal address safe to fetch. Checked here rather
    # than only when the tool is saved, because the check that counts is
    # the one immediately before the request.
    unsafe = rejection_reason(url)
    if unsafe:
        logger.warning(f"[TOOL] {tool.name}: refused to call {url} — {unsafe}")
        return {
            "ok": False,
            "error": "blocked_url",
            "message": "That system could not be reached. Tell the caller plainly rather than guessing an answer.",
        }

    # str().replace(): a header value carrying a newline splits one request
    # into two as far as some servers are concerned. These values are
    # templated from model-supplied arguments like everything else, so the
    # possibility is real rather than theoretical.
    headers = {
        k: str(v).replace("\r", "").replace("\n", "")
        for k, v in _render_map(tool.headers, args).items()
    }
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

    # Parsed in FULL, then bounded by `_capped` at the point it goes to the
    # model — never truncated first. See _capped's docstring: truncating
    # before parsing turned every large-but-perfectly-valid JSON response
    # into "nothing was found."
    text = response.text
    try:
        payload: Any = json.loads(text) if text.strip() else None
    except ValueError:
        payload = text[:MAX_RESPONSE_CHARS]

    if response.status_code >= 400:
        logger.warning(f"[TOOL] {tool.name}: HTTP {response.status_code}")
        result = {
            "ok": False,
            "error": f"http_{response.status_code}",
            "status": response.status_code,
            "data": _capped(payload),
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
        result = {"ok": True, "status": response.status_code, "data": _capped(payload)}
        if fields is not None:
            result["fields"] = fields
        if tool.payment.enabled:
            await _track_payment_session(tool, payload, result)

    if cache_key is not None:
        call_context.current().cache[cache_key] = result
    return result


async def _track_payment_session(tool: BotTool, payload: Any, result: dict[str, Any]) -> None:
    """Task 3.7 — a payment link was just created; remember it against this
    live call so a later webhook can find its way back to the conversation.

    Never raises and never fails the tool call: the link itself was
    generated successfully by this point (the caller's actual request), and
    a tracking-record failure must not turn a working payment link into a
    reported failure — it only means the automatic on-call confirmation
    won't be able to find this one, which is degraded, not broken.
    """
    fields = {
        "reference": _resolve_path(payload, tool.payment.reference_field),
        "amount": _resolve_path(payload, tool.payment.amount_field),
        "link_url": _resolve_path(payload, tool.payment.link_field),
    }
    if not fields["reference"] or not fields["link_url"]:
        logger.warning(
            f"[PAYMENT] {tool.name}: create-link response did not have a "
            f"reference and a link at the configured paths — check "
            f"reference_field/link_field against what the provider actually "
            f"returned. Nothing tracked; the link itself is still valid."
        )
        result["message"] = (
            result.get("message", "")
            + " The link was created. Read it out or send it, but tell the caller "
            "you cannot confirm automatically when it is paid."
        )
        return

    try:
        from app.models.payment import PaymentSession
        from app.pipeline import call_context

        ctx = call_context.current()
        session = PaymentSession(
            reference=str(fields["reference"]),
            bot_id=ctx.bot_id or "",
            user_id=ctx.user_id or "",
            pc_id=ctx.pc_id or "",
            tool_id=str(tool.id or ""),
            amount=str(fields["amount"] or ""),
            link_url=str(fields["link_url"]),
        )
        await session.insert()
        logger.info(f"[PAYMENT] Tracking {session.reference} for pc_id={ctx.pc_id or '(none)'}")
        result["message"] = (
            result.get("message", "")
            + " Send this link to the caller now. You will be told automatically "
            "the moment it is paid — do not say it is confirmed until then."
        )
    except Exception as e:
        logger.warning(f"[PAYMENT] {tool.name}: could not save a tracking record: {type(e).__name__}: {e}")
        result["message"] = (
            result.get("message", "")
            + " The link was created. Read it out or send it, but tell the caller "
            "you cannot confirm automatically when it is paid."
        )


async def _check_approval_gate(tool: BotTool, args: dict[str, Any]) -> dict[str, Any] | None:
    """Task 3.10 — before the action runs at all, not after.

    Returns None when the tool may proceed normally. Returns a result dict
    when it may not: the underlying HTTP call never happens, and instead a
    PendingApproval record is created for a person to decide later.

    The amount is read from whichever declared parameter the tool names
    (`amount_parameter`) — the same argument the model filled in from what
    the caller said. If it's missing or not a number, this fails CLOSED:
    approval is required rather than skipped, because the whole point of
    this gate is that a big action should never slip through, and an
    unparseable amount is exactly the case where "let it through" would be
    the wrong default.
    """
    if not tool.approval.enabled:
        return None

    raw = args.get(tool.approval.amount_parameter)
    try:
        amount = float(raw)
        unparseable = False
    except (TypeError, ValueError):
        amount = tool.approval.threshold  # for display only; see docstring
        unparseable = True

    if not unparseable and amount <= tool.approval.threshold:
        return None

    from app.models.approval import PendingApproval
    from app.pipeline import call_context

    ctx = call_context.current()
    try:
        approval = PendingApproval(
            tool_id=str(tool.id or ""), bot_id=ctx.bot_id or "", user_id=ctx.user_id or "",
            pc_id=ctx.pc_id or "", tool_name=tool.name, arguments=args,
            amount=amount, threshold=tool.approval.threshold,
        )
        await approval.insert()
    except Exception as e:
        # The action must NOT run un-approved just because the approval
        # record itself failed to save — that would silently defeat the
        # whole feature. Refuse instead, with something the model can say.
        logger.error(f"[APPROVAL] Could not create a pending approval for {tool.name}: {e}")
        return {
            "ok": False,
            "error": "approval_unavailable",
            "message": (
                "This action needs approval and that could not be set up right now. "
                "Tell the caller it cannot be completed on this call and to try again "
                "shortly."
            ),
        }

    logger.info(
        f"[APPROVAL] {tool.name}: {amount} > threshold {tool.approval.threshold} "
        f"— queued approval {approval.id}, action not yet run"
    )
    return {
        "ok": True,
        "pending_approval": True,
        "approval_id": str(approval.id),
        "message": (
            f"This is above the amount that can be approved automatically "
            f"({tool.approval.threshold}) and has been sent to a person to review. "
            "Tell the caller it needs sign-off and they will hear back separately — "
            "do not make them wait on this call, and do not say it is done."
        ),
    }


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

        # Task 3.10 — FIRST, before anything else can dispatch the work.
        #
        # This used to sit below the long-running branch, which meant a
        # tool configured as both long-running (3.3) and approval-gated
        # (3.10) handed itself to the background runner and executed for
        # real without any approval ever being asked for. Nothing warned
        # about it: the caller heard the normal "I'm working on it", and
        # the action simply happened. Those two settings are most likely
        # to be combined on exactly the tools this gate exists for — a
        # large refund against a slow payment provider is both — so the
        # bypass sat where it would do the most damage.
        #
        # The ordering is the fix and also the invariant: the gate decides
        # whether the underlying action may happen AT ALL, so nothing that
        # can cause it to happen may run before the gate does.
        gated = await _check_approval_gate(tool, args)
        if gated is not None:
            if saga is not None:
                await saga.record(tool.name, tool, args, gated)
            await params.result_callback(gated)
            return

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
) -> tuple[list[Any], bool, bool, bool, bool]:
    """Every enabled tool this bot has, ready to hand to the LLM context.

    Returns (tools, has_background, has_undo, has_payment, has_approval).
    The flags decide which prompt rules this bot needs — background
    acknowledgements (3.3), partial rollback (3.4), never asking for card
    details (3.7), and never claiming a gated action is done (3.10) — so a
    bot without such tools does not carry an explanation of them on every
    turn.

    Falls back to the built-in set when a bot has configured nothing, so
    every bot that existed before this task keeps the tools it had. A bot
    with tools configured gets exactly those — which is what makes the
    prompt smaller and the model's choice easier, rather than every bot
    carrying every tool ever written.
    """
    builtins = _builtin_tools()
    if not bot_id:
        return list(builtins.values()), False, False, False, False

    try:
        records = await BotTool.find(BotTool.bot_id == bot_id, BotTool.enabled == True).to_list()  # noqa: E712
    except Exception as e:
        # A tool-loading failure must not take the call down: the caller can
        # still have a conversation, just without tools.
        logger.warning(f"[TOOLS] Could not load tools for bot {bot_id}: {e}")
        return list(builtins.values()), False, False, False, False

    if not records:
        logger.info(f"[TOOLS] Bot {bot_id} has none configured — using the {len(builtins)} built-ins")
        return list(builtins.values()), False, False, False, False

    loaded: list[Any] = []
    # A tool's name becomes a function name in the schema sent to the LLM
    # provider, and two functions with the same name is not a thing that
    # schema can express — the provider either rejects the request outright
    # or silently keeps one of them, which would present as "my second tool
    # never gets called" with nothing in the logs to explain it. Nothing
    # stops a customer naming two tools the same, so the first one wins and
    # the collision is said out loud.
    used_names: set[str] = set()
    for record in records:
        if record.name in used_names:
            logger.warning(
                f"[TOOLS] Bot {bot_id} has more than one enabled tool named "
                f"{record.name!r} — only the first is being offered to the model. "
                f"Rename or disable the duplicate."
            )
            continue
        if record.kind == "builtin":
            fn = builtins.get(record.builtin)
            if fn is None:
                logger.warning(f"[TOOLS] {record.name}: no built-in named {record.builtin!r}")
                continue
            loaded.append(fn)
        else:
            loaded.append(to_function_schema(record, jobs, saga))
        used_names.add(record.name)

    has_background = any(r.long_running and r.kind == "http" for r in records)
    has_undo = any(r.kind == "http" and r.undo and r.undo.url for r in records)
    has_payment = any(r.kind == "http" and r.payment and r.payment.enabled for r in records)
    has_approval = any(r.kind == "http" and r.approval and r.approval.enabled for r in records)
    logger.info(
        f"[TOOLS] Bot {bot_id}: loaded {len(loaded)} configured tool(s)"
        + (" (one or more run in the background)" if has_background else "")
    )
    return loaded, has_background, has_undo, has_payment, has_approval


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
