"""Task 3.1 — tools as configuration rather than code.

Before this, TOOLS was one global list and every bot got all of it. Adding
a tool for one customer meant editing Python and deploying, which does not
scale past a handful of customers — and it also meant a Hindi tutor bot
carried an order-lookup tool it would never use, as prompt noise on every
turn.

What these tests pin, in order of how much it would hurt to lose:

  1. A tool that exists only as a database row reaches the model with a
     usable schema and a working handler. That is the whole task.
  2. Credentials never come back out of the API, and are not stored in
     plain text.
  3. A bot with nothing configured still gets the built-ins, so every bot
     created before this task keeps working.
  4. The generic HTTP tool substitutes values, authenticates, and turns
     every failure into something the model can say out loud rather than
     an exception that becomes dead air on a live call.
"""

import pytest
from pipecat.adapters.schemas.function_schema import FunctionSchema

from app.core.crypto import decrypt_secret, encrypt_secret, mask_secret
from app.models.bot_tool import BotTool, ToolAuth, ToolParameter
from app.pipeline import call_context
from app.services import tool_registry
from app.services.tool_registry import (
    _apply_auth,
    _render,
    _render_map,
    _resolve_path,
    call_http_tool,
    to_function_schema,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _http_tool(**over) -> BotTool:
    base = dict(
        bot_id="bot-1",
        name="check_stock",
        description="Check whether an item is in stock.",
        kind="http",
        method="GET",
        url="https://api.example.com/stock/{sku}",
        parameters=[ToolParameter(name="sku", description="The item's SKU code")],
    )
    base.update(over)
    return BotTool(**base)


# --- 1. a row becomes a tool the model can call ----------------------------

def test_a_database_row_becomes_a_usable_tool_schema():
    """The task in one assertion: no Python function exists for this tool."""
    schema = to_function_schema(_http_tool())

    assert isinstance(schema, FunctionSchema)
    assert schema.name == "check_stock"
    assert schema.description == "Check whether an item is in stock."
    assert schema.properties["sku"]["type"] == "string"
    assert schema.required == ["sku"]
    assert schema.handler is not None, (
        "without a handler the model can see the tool but calling it does nothing"
    )


def test_optional_parameters_are_not_demanded_of_the_model():
    tool = _http_tool(parameters=[
        ToolParameter(name="sku"),
        ToolParameter(name="warehouse", required=False),
    ])
    properties, required = tool.json_schema()
    assert set(properties) == {"sku", "warehouse"}
    assert required == ["sku"]


def test_a_tool_name_must_be_a_valid_function_name():
    """It becomes a function name in the schema sent to the provider, so a
    name with spaces is rejected here rather than at the provider."""
    with pytest.raises(ValueError):
        _http_tool(name="check stock")
    with pytest.raises(ValueError):
        BotTool(bot_id="b", name="ok", description="d", parameters=[ToolParameter(name="order id")])


def test_an_unknown_http_method_is_refused():
    with pytest.raises(ValueError):
        _http_tool(method="FETCH")


def test_a_pasted_in_leading_space_on_the_url_is_trimmed():
    """Found live 2026-09-05: a leading space is invisible in the form's
    text box but not to httpx — a URL that starts with a space no longer
    starts with "https://" as far as the request library is concerned, and
    it refuses to send the request at all (UnsupportedProtocol), instantly,
    before it ever reaches the customer's API. name and method were already
    trimmed on save; url was the one field that wasn't, and it's the one
    most often arrived at by copy-paste rather than typing."""
    tool = _http_tool(url="  https://api.example.com/stock/{sku}  ")
    assert tool.url == "https://api.example.com/stock/{sku}"


def test_the_undo_urls_leading_space_is_also_trimmed():
    """The same mistake, the same fix, on task 3.4's undo URL — filled in
    by hand the same way as the main one."""
    from app.models.bot_tool import ToolUndo

    tool = _http_tool(undo=ToolUndo(url=" https://api.example.com/cancel "))
    assert tool.undo.url == "https://api.example.com/cancel"


async def test_a_tool_saved_before_the_fix_is_still_protected_at_call_time(monkeypatch):
    """Belt and suspenders: a row written to the database before this
    validator existed still has the raw leading space sitting in storage.
    call_http_tool strips it too, so an already-saved tool is fixed the
    moment this deploys — nobody has to notice and re-edit it."""
    tool = _http_tool()
    # Beanie's own validation runs on construction, which is exactly what
    # would strip it — so the raw value is forced back in afterwards to
    # simulate a row that predates the fix, the way `Bot.get()` would load
    # one straight off disk.
    object.__setattr__(tool, "url", "  https://api.example.com/stock/{sku}")

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = "{}"

    async def _fake_request(method, url, **kwargs):
        captured["url"] = url
        return _FakeResponse()

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        request = staticmethod(_fake_request)

    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **kw: _FakeClient())

    result = await call_http_tool(tool, {"sku": "ABC"})

    assert result["ok"] is True
    assert captured["url"] == "https://api.example.com/stock/ABC"


# --- 2. credentials -------------------------------------------------------

def test_a_credential_is_recoverable_but_not_stored_in_the_clear():
    """Unlike a password, this has to be sent to the customer's API, so it
    is encrypted rather than hashed — but must not sit in the database as
    plain text."""
    secret = "sk_live_abc123XYZ"
    stored = encrypt_secret(secret)

    assert secret not in stored
    assert stored != secret
    assert decrypt_secret(stored) == secret


def test_an_unreadable_credential_degrades_instead_of_raising():
    """SECRET_KEY rotated, or a hand-edited row. The call should fail as an
    unauthorised request to the customer's API, which is legible, not as an
    exception from somewhere unrelated."""
    assert decrypt_secret("not-a-valid-token") == ""
    assert decrypt_secret("") == ""


def test_masking_shows_enough_to_recognise_and_no_more():
    masked = mask_secret("sk_live_abc123XYZ")
    assert masked.endswith("3XYZ")
    assert "sk_live" not in masked
    assert mask_secret("") == ""


@pytest.mark.parametrize(
    "kind,name,expect_header,expect_query",
    [
        ("bearer", "", ("Authorization", "Bearer s3cret"), None),
        ("header", "X-Api-Key", ("X-Api-Key", "s3cret"), None),
        ("query", "api_key", None, ("api_key", "s3cret")),
    ],
)
def test_the_credential_goes_where_the_customers_api_expects_it(
    kind, name, expect_header, expect_query
):
    tool = _http_tool(auth=ToolAuth(kind=kind, name=name, secret_encrypted=encrypt_secret("s3cret")))
    headers, params = {}, {}
    _apply_auth(tool, headers, params)

    if expect_header:
        assert headers[expect_header[0]] == expect_header[1]
    if expect_query:
        assert params[expect_query[0]] == expect_query[1]


def test_no_auth_adds_nothing():
    headers, params = {}, {}
    _apply_auth(_http_tool(), headers, params)
    assert headers == {} and params == {}


# --- 3. existing bots keep working ----------------------------------------

async def test_a_bot_with_nothing_configured_still_gets_the_builtins():
    """Every bot predates this task. None of them would have tools at all
    if an empty configuration meant an empty toolset."""
    tools, *_ = await tool_registry.load_tools_for_bot("a-bot-with-no-tools-configured")
    names = [getattr(t, "__name__", getattr(t, "name", "")) for t in tools]
    assert "get_current_datetime" in names, names


async def test_no_bot_id_still_gets_the_builtins():
    tools, *_ = await tool_registry.load_tools_for_bot(None)
    assert tools, "a bot without an id must still be able to use tools"


# --- 4. the generic HTTP tool ---------------------------------------------

def test_placeholders_are_filled_from_the_models_arguments():
    assert _render("https://api.x.com/orders/{id}", {"id": "A-99"}) == "https://api.x.com/orders/A-99"


def test_braces_that_are_not_parameters_are_left_alone():
    """A customer's URL or JSON body may contain braces of its own. Using
    str.format here would raise or misread them."""
    assert _render("/a/{id}/{not_a_param}", {"id": "1"}) == "/a/1/{not_a_param}"


def test_substitution_reaches_nested_body_values():
    rendered = _render_map({"order": {"id": "{id}"}, "n": 3}, {"id": "A-1"})
    assert rendered == {"order": {"id": "A-1"}, "n": 3}


class _Resp:
    def __init__(self, status=200, text='{"in_stock": true}'):
        self.status_code, self.text = status, text


class _Client:
    """Stands in for httpx, recording what the tool actually sent."""

    def __init__(self, response=None, raise_with=None):
        self.response, self.raise_with, self.seen = response or _Resp(), raise_with, {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kw):
        self.seen = {"method": method, "url": url, **kw}
        if self.raise_with:
            raise self.raise_with
        return self.response


async def test_a_successful_call_returns_parsed_data(monkeypatch):
    client = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)

    result = await call_http_tool(_http_tool(), {"sku": "ABC-1"})

    assert result["ok"] is True
    assert result["data"] == {"in_stock": True}
    assert client.seen["url"] == "https://api.example.com/stock/ABC-1"


async def test_a_timeout_becomes_something_the_bot_can_say(monkeypatch):
    """An exception here would reach the caller as dead air. The model needs
    words instead."""
    monkeypatch.setattr(
        tool_registry.httpx, "AsyncClient",
        lambda **k: _Client(raise_with=tool_registry.httpx.TimeoutException("slow")),
    )
    result = await call_http_tool(_http_tool(), {"sku": "X"})

    assert result["ok"] is False
    assert result["error"] == "timeout"
    assert result["message"], "the model was given nothing to say"


async def test_an_unreachable_host_does_not_raise(monkeypatch):
    monkeypatch.setattr(
        tool_registry.httpx, "AsyncClient",
        lambda **k: _Client(raise_with=RuntimeError("dns")),
    )
    result = await call_http_tool(_http_tool(), {"sku": "X"})
    assert result["ok"] is False and result["error"] == "unreachable"


@pytest.mark.parametrize("status,hint", [(404, "confirm"), (500, "unavailable")])
async def test_client_and_server_errors_are_described_differently(monkeypatch, status, hint):
    """A 4xx is usually the caller's input; a 5xx is the customer's system.
    Telling the model the difference is what stops it saying "your order
    number is wrong" when the API is simply down."""
    monkeypatch.setattr(
        tool_registry.httpx, "AsyncClient",
        lambda **k: _Client(response=_Resp(status=status, text="{}")),
    )
    result = await call_http_tool(_http_tool(), {"sku": "X"})

    assert result["ok"] is False
    assert result["status"] == status
    assert hint in result["message"].lower()


async def test_a_non_json_response_is_still_usable(monkeypatch):
    monkeypatch.setattr(
        tool_registry.httpx, "AsyncClient",
        lambda **k: _Client(response=_Resp(text="in stock")),
    )
    result = await call_http_tool(_http_tool(), {"sku": "X"})
    assert result["ok"] is True and result["data"] == "in stock"


async def test_a_huge_response_is_truncated(monkeypatch):
    """A customer API returning a large document would otherwise push the
    conversation itself out of the model's context window."""
    monkeypatch.setattr(
        tool_registry.httpx, "AsyncClient",
        lambda **k: _Client(response=_Resp(text="x" * 50_000)),
    )
    result = await call_http_tool(_http_tool(), {"sku": "X"})
    assert len(str(result["data"])) <= tool_registry.MAX_RESPONSE_CHARS + 10


async def test_the_handler_hands_its_result_to_pipecat(monkeypatch):
    """The generated handler must call result_callback — a handler that
    returns instead leaves the model waiting forever."""
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: _Client())

    delivered = {}

    class _Params:
        arguments = {"sku": "ABC-1"}

        async def result_callback(self, result):
            delivered.update(result)

    await to_function_schema(_http_tool()).handler(_Params())
    assert delivered.get("ok") is True


# --- 5. the API: ownership, and credentials that never come back ----------
#
# Widening the surface (a new resource, a new secret) is exactly when the
# task 2.6 isolation guarantee is most worth re-checking, so it is asserted
# here against the real routes rather than assumed from the dependency.

from tests.conftest import auth_headers  # noqa: E402


async def _make_bot(client, token, name="Tools bot"):
    resp = await client.post("/bots/", json={"name": name}, headers=auth_headers(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


TOOL_BODY = {
    "name": "check_stock",
    "description": "Check whether an item is in stock.",
    "kind": "http",
    "method": "GET",
    "url": "https://api.example.com/stock/{sku}",
    "parameters": [{"name": "sku", "type": "string", "description": "SKU code", "required": True}],
    "auth": {"kind": "bearer", "name": "", "secret": "sk_live_SUPERSECRET"},
}


async def test_a_configured_tool_round_trips_without_leaking_the_secret(client, user_a_token):
    bot_id = await _make_bot(client, user_a_token)

    created = await client.post(f"/bots/{bot_id}/tools/", json=TOOL_BODY, headers=auth_headers(user_a_token))
    assert created.status_code == 201, created.text
    body = created.json()

    assert "sk_live_SUPERSECRET" not in created.text, "the API echoed the credential back"
    assert body["auth"]["has_secret"] is True
    assert body["auth"]["secret_masked"].endswith("CRET")

    listed = await client.get(f"/bots/{bot_id}/tools/", headers=auth_headers(user_a_token))
    assert "sk_live_SUPERSECRET" not in listed.text
    assert [t["name"] for t in listed.json()] == ["check_stock"]


async def test_the_stored_secret_is_the_one_the_tool_will_send(client, user_a_token):
    """Masked in the API, intact in the database — otherwise the tool would
    authenticate with a masked string."""
    bot_id = await _make_bot(client, user_a_token, "Secret bot")
    await client.post(f"/bots/{bot_id}/tools/", json=TOOL_BODY, headers=auth_headers(user_a_token))

    stored = await BotTool.find_one(BotTool.bot_id == bot_id)
    assert decrypt_secret(stored.auth.secret_encrypted) == "sk_live_SUPERSECRET"
    assert "sk_live_SUPERSECRET" not in stored.auth.secret_encrypted


async def test_editing_a_tool_without_resending_the_secret_keeps_it(client, user_a_token):
    """The UI shows a masked value; saving a URL change must not wipe the key."""
    bot_id = await _make_bot(client, user_a_token, "Edit bot")
    tool_id = (await client.post(f"/bots/{bot_id}/tools/", json=TOOL_BODY,
                                 headers=auth_headers(user_a_token))).json()["id"]

    edit = {**TOOL_BODY, "url": "https://api.example.com/v2/stock/{sku}"}
    edit["auth"] = {"kind": "bearer", "name": ""}          # no "secret" key at all
    resp = await client.patch(f"/bots/{bot_id}/tools/{tool_id}", json=edit,
                              headers=auth_headers(user_a_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["url"].endswith("/v2/stock/{sku}")

    stored = await BotTool.get(stored_id(tool_id))
    assert decrypt_secret(stored.auth.secret_encrypted) == "sk_live_SUPERSECRET"


def stored_id(tool_id):
    from beanie import PydanticObjectId
    return PydanticObjectId(tool_id)


async def test_another_user_cannot_see_or_touch_your_tools(client, user_a_token, user_b_token):
    bot_id = await _make_bot(client, user_a_token, "Private bot")
    tool_id = (await client.post(f"/bots/{bot_id}/tools/", json=TOOL_BODY,
                                 headers=auth_headers(user_a_token))).json()["id"]

    for method, path in [
        ("get", f"/bots/{bot_id}/tools/"),
        ("delete", f"/bots/{bot_id}/tools/{tool_id}"),
        ("post", f"/bots/{bot_id}/tools/{tool_id}/test"),
    ]:
        resp = await getattr(client, method)(path, headers=auth_headers(user_b_token))
        assert resp.status_code in (403, 404), f"{method} {path} returned {resp.status_code}"


async def test_a_tool_id_from_another_bot_is_refused(client, user_a_token):
    """Same owner, wrong bot — the bot check alone would let this through."""
    bot_a = await _make_bot(client, user_a_token, "Bot A")
    bot_b = await _make_bot(client, user_a_token, "Bot B")
    tool_id = (await client.post(f"/bots/{bot_a}/tools/", json=TOOL_BODY,
                                 headers=auth_headers(user_a_token))).json()["id"]

    resp = await client.delete(f"/bots/{bot_b}/tools/{tool_id}", headers=auth_headers(user_a_token))
    assert resp.status_code == 404


async def test_two_tools_on_one_bot_cannot_share_a_name(client, user_a_token):
    """Duplicate function names in one schema is an error at the provider,
    and the model could not tell them apart anyway."""
    bot_id = await _make_bot(client, user_a_token, "Dupe bot")
    await client.post(f"/bots/{bot_id}/tools/", json=TOOL_BODY, headers=auth_headers(user_a_token))
    again = await client.post(f"/bots/{bot_id}/tools/", json=TOOL_BODY, headers=auth_headers(user_a_token))
    assert again.status_code == 409


async def test_configured_tools_replace_the_builtins_for_that_bot(client, user_a_token):
    """The prompt-size half of this task: a bot carries its own tools, not
    every tool ever written."""
    bot_id = await _make_bot(client, user_a_token, "Configured bot")
    await client.post(f"/bots/{bot_id}/tools/", json=TOOL_BODY, headers=auth_headers(user_a_token))

    tools, *_ = await tool_registry.load_tools_for_bot(bot_id)
    names = [getattr(t, "__name__", getattr(t, "name", "")) for t in tools]
    assert names == ["check_stock"], names


async def test_a_disabled_tool_is_not_offered_to_the_model(client, user_a_token):
    bot_id = await _make_bot(client, user_a_token, "Disabled bot")
    tool_id = (await client.post(f"/bots/{bot_id}/tools/", json=TOOL_BODY,
                                 headers=auth_headers(user_a_token))).json()["id"]
    await client.patch(f"/bots/{bot_id}/tools/{tool_id}", json={**TOOL_BODY, "enabled": False},
                       headers=auth_headers(user_a_token))

    tools, *_ = await tool_registry.load_tools_for_bot(bot_id)
    assert "check_stock" not in [getattr(t, "name", "") for t in tools]


async def test_the_test_button_reports_a_real_failure(client, user_a_token, monkeypatch):
    """Its whole point is finding out a URL or key is wrong without placing
    a phone call — so a failure has to come back as a readable result."""
    bot_id = await _make_bot(client, user_a_token, "Test-button bot")
    tool_id = (await client.post(f"/bots/{bot_id}/tools/", json=TOOL_BODY,
                                 headers=auth_headers(user_a_token))).json()["id"]

    monkeypatch.setattr(
        tool_registry.httpx, "AsyncClient",
        lambda **k: _Client(raise_with=RuntimeError("no such host")),
    )
    resp = await client.post(f"/bots/{bot_id}/tools/{tool_id}/test",
                             json={"sku": "ABC"}, headers=auth_headers(user_a_token))

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert resp.json()["error"] == "unreachable"


# --- 5. task 3.6, the lookup template ---------------------------------------
#
# "Where is my order" is the most common support question there is, and
# the manual's own tests for this template are: any API shape works with
# configuration alone, a slow system fails fast rather than freezing the
# call, and asking the same thing twice in one call doesn't ask the
# customer's system twice.

@pytest.fixture(autouse=True)
def _clear_call_context():
    """The cache lives on call_context's per-call state — start every test
    from a known-empty one so a leftover cache entry from a previous test
    can't produce a false pass."""
    call_context.clear()
    yield
    call_context.clear()


def test_a_deeply_nested_field_is_pulled_out_by_its_dotted_path():
    payload = {"data": {"order": {"status": "shipped"}}}
    assert _resolve_path(payload, "data.order.status") == "shipped"


def test_a_field_that_is_not_there_for_this_record_is_none_not_an_error():
    """A missing tracking number on a not-yet-shipped order is a normal
    case, not a bug in the mapping."""
    assert _resolve_path({"data": {}}, "data.order.status") is None
    assert _resolve_path({"data": None}, "data.order.status") is None


async def test_a_response_is_normalised_through_the_configured_field_map(monkeypatch):
    """The whole point: the model reads a name it can rely on regardless of
    how the real API nests it."""
    tool = _http_tool(field_map={"status": "data.order.delivery_status", "eta": "data.order.eta"})
    monkeypatch.setattr(
        tool_registry.httpx, "AsyncClient",
        lambda **k: _Client(response=_Resp(
            text='{"data": {"order": {"delivery_status": "out_for_delivery", "eta": "today"}}}'
        )),
    )

    result = await call_http_tool(tool, {"sku": "X"})

    assert result["fields"] == {"status": "out_for_delivery", "eta": "today"}
    assert result["data"], "the raw response should still be there for anything not mapped"


async def test_a_tool_with_no_field_map_behaves_exactly_as_before(monkeypatch):
    """Every tool from before 3.6 has an empty field_map. It must not
    suddenly grow a "fields" key it never asked for."""
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: _Client())
    result = await call_http_tool(_http_tool(), {"sku": "X"})
    assert "fields" not in result


async def test_a_200_with_every_mapped_field_empty_is_reported_as_not_found(monkeypatch):
    """Some APIs answer "no such record" with a 200 and an empty body
    instead of a 404 — the status code alone can't see that, only the
    mapping can."""
    tool = _http_tool(field_map={"status": "data.order.status"})
    monkeypatch.setattr(
        tool_registry.httpx, "AsyncClient",
        lambda **k: _Client(response=_Resp(text='{"data": {}}')),
    )

    result = await call_http_tool(tool, {"sku": "does-not-exist"})

    assert result["ok"] is True  # the HTTP call itself succeeded
    assert result["found"] is False
    assert "not" in result["message"].lower()


async def test_one_mapped_field_present_is_not_treated_as_not_found(monkeypatch):
    """Only "every mapped field came back empty" means not-found — a
    genuinely sparse but real record must not be reported as missing."""
    tool = _http_tool(field_map={"status": "data.status", "eta": "data.eta"})
    monkeypatch.setattr(
        tool_registry.httpx, "AsyncClient",
        lambda **k: _Client(response=_Resp(text='{"data": {"status": "processing"}}')),
    )

    result = await call_http_tool(tool, {"sku": "X"})

    assert result.get("found") is not False
    assert result["fields"]["status"] == "processing"


async def test_a_lookup_tool_can_set_a_shorter_timeout_than_the_default(monkeypatch):
    """The manual's own number for this template: around three seconds,
    because it is far better to say the system is not responding than to
    leave the caller in silence."""
    tool = _http_tool(timeout_seconds=3.0)
    seen = {}

    def _fake_client(**kw):
        seen.update(kw)
        return _Client()

    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", _fake_client)
    await call_http_tool(tool, {"sku": "X"})

    assert seen["timeout"] == 3.0


async def test_a_tool_saved_before_3_6_keeps_the_old_default_timeout(monkeypatch):
    seen = {}

    def _fake_client(**kw):
        seen.update(kw)
        return _Client()

    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", _fake_client)
    await call_http_tool(_http_tool(), {"sku": "X"})  # no timeout_seconds override

    assert seen["timeout"] == tool_registry.HTTP_TIMEOUT_SECONDS


async def test_a_repeated_get_in_one_call_is_not_asked_for_twice(monkeypatch):
    """A caller circling back to the same question, or the model
    double-checking, must not cost the customer's system a second request."""
    call_context.set_call(bot_id="b1", session_id="s1")

    class _CountingClient(_Client):
        def __init__(self):
            super().__init__()
            self.n = 0

        async def request(self, method, url, **kw):
            self.n += 1
            self.seen = {"method": method, "url": url, **kw}
            return _Resp(text='{"n": 1}')

    client = _CountingClient()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)

    first = await call_http_tool(_http_tool(), {"sku": "ABC-1"})
    second = await call_http_tool(_http_tool(), {"sku": "ABC-1"})

    assert first == second
    assert client.n == 1, "a second identical GET reached the customer's system"


async def test_different_arguments_are_not_served_from_each_others_cache(monkeypatch):
    call_context.set_call(bot_id="b1", session_id="s1")

    class _VaryingClient(_Client):
        def __init__(self):
            super().__init__()
            self.n = 0

        async def request(self, method, url, **kw):
            self.n += 1
            self.seen = {"method": method, "url": url, **kw}
            return _Resp(text='{"call_number": ' + str(self.n) + '}')

    client = _VaryingClient()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)

    first = await call_http_tool(_http_tool(), {"sku": "ABC-1"})
    second = await call_http_tool(_http_tool(), {"sku": "DIFFERENT-SKU"})

    assert first["data"] != second["data"], "a different lookup was served a stale answer"
    assert client.n == 2


async def test_a_write_is_never_served_from_the_cache(monkeypatch):
    """Caching is safe only for a request with no side effect. A booking or
    payment tool re-running because its arguments matched an earlier call
    in the same conversation would be a serious bug, not an optimisation."""
    call_context.set_call(bot_id="b1", session_id="s1")

    class _CountingClient(_Client):
        def __init__(self):
            super().__init__()
            self.n = 0

        async def request(self, method, url, **kw):
            self.n += 1
            self.seen = {"method": method, "url": url, **kw}
            return _Resp(text='{"booked": true}')

    client = _CountingClient()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    tool = _http_tool(method="POST")

    await call_http_tool(tool, {"sku": "ABC-1"})
    await call_http_tool(tool, {"sku": "ABC-1"})

    assert client.n == 2, "a POST was cached — a real action would have been skipped"


async def test_a_failed_lookup_is_not_cached(monkeypatch):
    """A 404 now can become a 200 a moment later (the order gets placed
    mid-call) — caching a failure risks telling the caller "not found"
    forever within one call."""
    call_context.set_call(bot_id="b1", session_id="s1")

    class _RecoveringClient(_Client):
        def __init__(self):
            super().__init__()
            self.n = 0

        async def request(self, method, url, **kw):
            self.n += 1
            self.seen = {"method": method, "url": url, **kw}
            if self.n == 1:
                return _Resp(status=404, text='{"message": "not found"}')
            return _Resp(text='{"found": true}')

    client = _RecoveringClient()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)

    first = await call_http_tool(_http_tool(), {"sku": "ABC-1"})
    second = await call_http_tool(_http_tool(), {"sku": "ABC-1"})

    assert first["ok"] is False
    assert second["ok"] is True
    assert client.n == 2, "the failed first attempt was wrongly cached"


async def test_the_cache_does_not_survive_between_two_different_calls(monkeypatch):
    """The cache is per-call, not global — a value cached for one caller
    must never answer a different caller's identical-looking question on a
    reused worker process."""
    class _CountingClient(_Client):
        def __init__(self):
            super().__init__()
            self.n = 0

        async def request(self, method, url, **kw):
            self.n += 1
            self.seen = {"method": method, "url": url, **kw}
            return _Resp(text='{"n": ' + str(self.n) + '}')

    client = _CountingClient()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)

    call_context.set_call(bot_id="b1", session_id="call-one")
    first = await call_http_tool(_http_tool(), {"sku": "ABC-1"})

    call_context.set_call(bot_id="b1", session_id="call-two")  # a fresh call starts
    second = await call_http_tool(_http_tool(), {"sku": "ABC-1"})

    assert first["data"] != second["data"], "one call's cache leaked into a different call"
    assert client.n == 2
