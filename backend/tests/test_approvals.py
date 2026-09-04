"""Task 3.10 — big actions wait for a person.

The manual's own reasoning is what every test here checks for: no company
will let an AI approve a large refund unsupervised. So the tests are
organised around the one thing that must never happen — the underlying
action running before a person says so — and around the honest edge
cases the manual doesn't spell out but a real deployment would hit:
approval declined, the amount unreadable, the tool edited out from under
a queued request.
"""

import pytest

from app.models.approval import PendingApproval
from app.models.bot_tool import BotTool
from app.pipeline import call_context
from app.services import tool_registry
from app.services.tool_registry import APPROVAL_RULE, to_function_schema
from tests.conftest import auth_headers

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _Resp:
    def __init__(self, status=200, text='{"ok": true, "refund_id": "R-1"}'):
        self.status_code, self.text = status, text


class _Client:
    def __init__(self, response=None):
        self.response, self.calls = response or _Resp(), 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kw):
        self.calls += 1
        return self.response


class _HandlerParams:
    """Stands in for pipecat's FunctionCallParams — the approval gate
    lives in the generated HANDLER (_http_handler's closure), not in
    call_http_tool itself, precisely so approvals.py's approve() can call
    call_http_tool directly to bypass it. Testing the gate means going
    through this same entry point, the way pipecat actually would."""

    def __init__(self, arguments):
        self.arguments = arguments
        self.result = None

    async def result_callback(self, result):
        self.result = result


async def _call_gated(tool: BotTool, args: dict) -> dict:
    params = _HandlerParams(args)
    await to_function_schema(tool).handler(params)
    return params.result


def _gated_tool(**over) -> BotTool:
    base = dict(
        bot_id="bot-1", name="issue_refund", description="Issue a refund.",
        kind="http", method="POST", url="https://api.test/refunds",
        parameters=[],
        approval={"enabled": True, "amount_parameter": "amount", "threshold": 100.0},
    )
    base.update(over)
    return BotTool(**base)


@pytest.fixture(autouse=True)
def _clean_context():
    call_context.clear()
    yield
    call_context.clear()


# --- the gate itself: nothing runs without approval -------------------------

async def test_an_amount_under_the_threshold_runs_normally(monkeypatch):
    client = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)

    result = await _call_gated(_gated_tool(), {"amount": 50})

    assert result["ok"] is True
    assert "pending_approval" not in result
    assert client.calls == 1, "an amount under the threshold should reach the customer's API"


async def test_an_amount_over_the_threshold_never_reaches_the_customers_api(monkeypatch):
    client = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    call_context.set_call(bot_id="bot-1", user_id="user-1")

    result = await _call_gated(_gated_tool(), {"amount": 500})

    assert result["ok"] is True
    assert result["pending_approval"] is True
    assert client.calls == 0, "the action ran before anyone approved it"


async def test_an_amount_exactly_at_the_threshold_does_not_require_approval(monkeypatch):
    """The manual's own examples are all strictly-above framing ("a large
    refund"); the boundary itself is arbitrary, but it has to be one thing
    consistently — at-or-under proceeds, over requires a person."""
    client = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    call_context.set_call(bot_id="bot-1", user_id="user-1")

    result = await _call_gated(_gated_tool(), {"amount": 100})

    assert "pending_approval" not in result


async def test_a_disabled_gate_never_creates_an_approval(monkeypatch):
    client = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    tool = _gated_tool(approval={"enabled": False, "amount_parameter": "amount", "threshold": 0})

    result = await _call_gated(tool, {"amount": 999999})

    assert "pending_approval" not in result
    assert client.calls == 1


async def test_an_unparseable_amount_requires_approval_rather_than_skipping_it(monkeypatch):
    """Fails CLOSED: the whole point is that a big action never slips
    through, and an amount the gate cannot even read is exactly the case
    where "let it through" would be the wrong default."""
    client = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: client)
    call_context.set_call(bot_id="bot-1", user_id="user-1")

    result = await _call_gated(_gated_tool(), {"amount": "not-a-number"})

    assert result["pending_approval"] is True
    assert client.calls == 0


async def test_a_missing_amount_argument_requires_approval():
    call_context.set_call(bot_id="bot-1", user_id="user-1")
    result = await _call_gated(_gated_tool(), {})  # no "amount" key at all
    assert result.get("pending_approval") is True


async def test_the_pending_approval_is_recorded_against_the_right_bot_and_call():
    call_context.set_call(bot_id="bot-42", user_id="user-9", pc_id="pc-live-1")
    tool = _gated_tool(bot_id="bot-42")

    result = await _call_gated(tool, {"amount": 250})

    approval = await PendingApproval.find_one(PendingApproval.amount == 250)
    assert approval is not None
    assert approval.bot_id == "bot-42"
    assert approval.user_id == "user-9"
    assert approval.pc_id == "pc-live-1"
    assert approval.status == "pending"
    assert approval.threshold == 100.0
    assert result["approval_id"] == str(approval.id)


async def test_the_model_is_told_not_to_promise_it_is_done():
    call_context.set_call(bot_id="bot-1", user_id="user-1")
    result = await _call_gated(_gated_tool(), {"amount": 500})
    assert "do not" in result["message"].lower()
    assert "sign-off" in result["message"].lower() or "person" in result["message"].lower()


# --- the saga's interaction with a pending approval -------------------------

async def test_a_pending_approval_is_not_treated_as_a_reversible_success():
    """A tool that happens to ALSO declare an undo must not have this
    treated as something that ran and could be rolled back — nothing ran."""
    from app.models.bot_tool import ToolUndo
    from app.pipeline.saga import TurnSaga

    saga = TurnSaga(announce=lambda s: _noop())
    tool = _gated_tool(undo=ToolUndo(url="https://api.test/refunds/cancel"))

    saga.begin(1)
    await saga.record("issue_refund", tool, {"amount": 500}, {"ok": True, "pending_approval": True})

    assert saga._pending_approval == ["issue_refund"]
    assert saga._steps == [], "nothing ran, so there is nothing to roll back"


async def test_describe_never_says_a_pending_action_succeeded():
    """A pending approval must land in its own bucket with its own honest
    sentence — not the "succeeded and still stands" one built for a
    genuinely completed irreversible action, which would be false here."""
    from app.pipeline.saga import TurnSaga

    saga = TurnSaga(announce=lambda s: _noop())
    saga.begin(2)
    await saga.record("issue_refund", _gated_tool(), {"amount": 500}, {"ok": True, "pending_approval": True})
    await saga.record("send_email", _gated_tool(name="send_email"), {}, {"ok": False, "error": "smtp_down"})

    summary = await saga.roll_back()
    assert summary["pending_approval"] == ["issue_refund"]
    assert "issue_refund" not in summary["left_standing"]

    sentence = saga.describe(summary)
    assert "issue_refund" in sentence
    assert "have NOT happened yet" in sentence
    assert "succeeded and CANNOT be undone" not in sentence.split("issue_refund")[0]


async def _noop():
    return None


# --- the prompt rule ---------------------------------------------------------

def test_the_approval_rule_tells_the_model_to_wait_and_not_lie():
    rule = APPROVAL_RULE.lower()
    assert "person" in rule
    assert "do not make them wait on this call" in rule
    assert "never say the action is done" in rule


def test_the_rule_is_only_added_for_a_bot_with_an_approval_tool():
    import inspect

    from app.pipeline import voice_pipeline

    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "if has_approval:" in source
    assert "APPROVAL_RULE" in source


# --- the approvals API -------------------------------------------------------

async def test_approving_runs_the_action_for_the_first_time(client, user_a_token, monkeypatch):
    tool = _gated_tool(bot_id="bot-api-1")
    await tool.insert()
    approval = PendingApproval(
        tool_id=str(tool.id), tool_name="issue_refund", bot_id="bot-api-1", user_id=str(
            await _user_id(client, user_a_token)
        ),
        arguments={"amount": 500}, amount=500, threshold=100,
    )
    await approval.insert()

    called = _Client(response=_Resp(text='{"ok": true, "refund_id": "R-99"}'))
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: called)

    resp = await client.post(f"/approvals/{approval.id}/approve", headers=auth_headers(user_a_token))

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert called.calls == 1, "approving must actually run the action"

    saved = await PendingApproval.get(approval.id)
    assert saved.executed is True
    assert saved.decided_by  # recorded who


async def test_denying_never_runs_the_action(client, user_a_token, monkeypatch):
    tool = _gated_tool(bot_id="bot-api-2")
    await tool.insert()
    approval = PendingApproval(
        tool_id=str(tool.id), tool_name="issue_refund", bot_id="bot-api-2",
        user_id=str(await _user_id(client, user_a_token)),
        arguments={"amount": 500}, amount=500, threshold=100,
    )
    await approval.insert()

    called = _Client()
    monkeypatch.setattr(tool_registry.httpx, "AsyncClient", lambda **k: called)

    resp = await client.post(f"/approvals/{approval.id}/deny", headers=auth_headers(user_a_token))

    assert resp.status_code == 200
    assert resp.json()["status"] == "denied"
    assert called.calls == 0, "a denied action must never run"


async def test_an_already_decided_approval_cannot_be_decided_again(client, user_a_token):
    tool = _gated_tool(bot_id="bot-api-3")
    await tool.insert()
    approval = PendingApproval(
        tool_id=str(tool.id), tool_name="issue_refund", bot_id="bot-api-3",
        user_id=str(await _user_id(client, user_a_token)),
        arguments={}, amount=500, threshold=100, status="approved",
    )
    await approval.insert()

    resp = await client.post(f"/approvals/{approval.id}/approve", headers=auth_headers(user_a_token))
    assert resp.status_code == 409


async def test_a_user_cannot_decide_someone_elses_approval(client, user_a_token, user_b_token):
    tool = _gated_tool(bot_id="bot-api-4")
    await tool.insert()
    approval = PendingApproval(
        tool_id=str(tool.id), tool_name="issue_refund", bot_id="bot-api-4",
        user_id=str(await _user_id(client, user_a_token)),
        arguments={}, amount=500, threshold=100,
    )
    await approval.insert()

    resp = await client.post(f"/approvals/{approval.id}/approve", headers=auth_headers(user_b_token))
    assert resp.status_code == 404


async def test_approving_when_the_tool_was_deleted_denies_automatically(client, user_a_token):
    approval = PendingApproval(
        tool_id="a-tool-id-that-was-deleted", tool_name="issue_refund", bot_id="bot-api-5",
        user_id=str(await _user_id(client, user_a_token)),
        arguments={}, amount=500, threshold=100,
    )
    await approval.insert()

    resp = await client.post(f"/approvals/{approval.id}/approve", headers=auth_headers(user_a_token))

    assert resp.status_code == 409
    saved = await PendingApproval.get(approval.id)
    assert saved.status == "denied", "an action whose tool no longer exists must never run"


async def test_the_list_endpoint_only_shows_the_users_own_approvals(client, user_a_token, user_b_token):
    tool = _gated_tool(bot_id="bot-api-6")
    await tool.insert()
    mine = PendingApproval(
        tool_id=str(tool.id), tool_name="issue_refund", bot_id="bot-api-6",
        user_id=str(await _user_id(client, user_a_token)),
        arguments={}, amount=500, threshold=100,
    )
    await mine.insert()

    resp = await client.get("/approvals/", headers=auth_headers(user_b_token))
    ids = [a["id"] for a in resp.json()]
    assert str(mine.id) not in ids


async def _user_id(client, token: str) -> str:
    """The approvals API scopes PendingApproval.user_id by the real Mongo
    id (see app/api/approvals.py), but the test token fixtures only hand
    back a JWT whose `sub` is the account's email (app/core/auth.py). This
    resolves the same way get_current_user itself does: decode the token,
    then look the user up by that email."""
    from jose import jwt

    from app.core.config import settings
    from app.models.user import User

    payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    user = await User.find_one(User.email == payload["sub"])
    return str(user.id)
