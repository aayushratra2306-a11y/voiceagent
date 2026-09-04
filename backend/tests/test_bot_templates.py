"""Task 3.9 — starting points instead of a blank instruction box.

The manual's own acceptance test: "a brand new user can pick a template
and have a genuinely good bot in under two minutes." The two-minutes part
is a frontend/UX claim this suite can't measure; what it CAN pin is the
half that would silently rot without a test — that every template is
actually usable, not placeholder content that looks fine in a list but
fails the moment a real bot tries to use it.
"""

import pytest

from app.api.bots import MAX_SYSTEM_PROMPT_LENGTH, _validate_system_prompt
from app.pipeline.bot_templates import TEMPLATES, get_template
from app.pipeline.tools import TOOLS

pytestmark = pytest.mark.asyncio(loop_scope="session")

_REAL_TOOL_NAMES = {fn.__name__ for fn in TOOLS}


# --- the manual's own steps, one test each ----------------------------------

def test_there_are_three_to_five_templates():
    """The manual's own number, step one: "three to five well-crafted
    instruction sets for common roles." Neither a token gesture (one or
    two) nor an unmaintainable sprawl."""
    assert 3 <= len(TEMPLATES) <= 5


def test_every_template_has_a_real_crafted_prompt():
    """Not a placeholder — the manual's whole premise is that a blank box
    produces bad bots, so a template that's basically still blank defeats
    the point. A genuinely written prompt runs to paragraphs, not a
    sentence."""
    for t in TEMPLATES:
        assert len(t.system_prompt) > 300, f"{t.id}'s prompt looks like a placeholder, not a crafted one"


def test_every_template_passes_the_real_validation_a_customer_would_hit():
    """The manual's step one output has to survive step three (added to
    the bot creation form) — a template that would be REJECTED by the
    same validation a customer's own prompt goes through is not usable."""
    for t in TEMPLATES:
        _validate_system_prompt(t.system_prompt)  # raises on failure


def test_no_template_prompt_is_anywhere_near_the_length_cap():
    """Comfortable headroom, not a validation that happens to scrape by —
    a customer editing the template (the manual's own step four) needs
    room to add to it, not immediately hit the cap."""
    for t in TEMPLATES:
        assert len(t.system_prompt) < MAX_SYSTEM_PROMPT_LENGTH * 0.6


def test_every_suggested_tool_is_a_real_function_that_exists():
    """The manual's step two: "include sensible tool selections." A name
    that doesn't resolve to anything real would silently give a customer
    zero working tools despite the template claiming to include some —
    exactly the kind of gap load_tools_for_bot's own warning-and-skip
    behaviour (services/tool_registry.py) would hide rather than surface."""
    for t in TEMPLATES:
        unknown = set(t.tools) - _REAL_TOOL_NAMES
        assert not unknown, f"{t.id} suggests tools that don't exist: {unknown}"


def test_no_template_suggests_every_tool_there_is():
    """The reason this task exists rather than just defaulting every new
    bot to task 3.1's fallback (every builtin, for a bot with nothing
    configured): a template is supposed to be curated FOR a role. A Tutor
    template offered the booking tools is the exact irrelevant-tool
    problem 3.1's own introduction was written to solve — this stays a
    strict subset as a smell test for template quality, not a bureaucratic
    rule."""
    for t in TEMPLATES:
        assert 0 < len(t.tools) < len(_REAL_TOOL_NAMES), (
            f"{t.id} lists {len(t.tools)} of {len(_REAL_TOOL_NAMES)} real tools "
            f"— curate a subset, or explain why every tool genuinely applies"
        )


def test_ids_and_names_are_unique():
    ids = [t.id for t in TEMPLATES]
    names = [t.name for t in TEMPLATES]
    assert len(ids) == len(set(ids)), "duplicate template id — the picker can't tell them apart"
    assert len(names) == len(set(names)), "duplicate template name — confusing in the picker"


def test_get_template_finds_a_real_one_and_returns_none_for_a_typo():
    assert get_template(TEMPLATES[0].id) is TEMPLATES[0]
    assert get_template("not-a-real-template-id") is None


# --- the manual's roles, by name -------------------------------------------

def test_a_booking_flavoured_template_uses_the_booking_tool_set():
    """One of the three named examples in the manual's own description
    ("Customer Support, Sales, Tutor") is support-shaped; scheduling is the
    other genuinely distinct shape this codebase already has a whole
    template FOR (task 3.5) — a starting-points feature that ignored its
    own most fully-built capability would be an odd omission."""
    booking_tools = {"check_availability", "book_appointment", "cancel_appointment", "reschedule_appointment"}
    assert any(booking_tools.issubset(set(t.tools)) for t in TEMPLATES), (
        "no template offers the booking tool set built in task 3.5"
    )


def test_a_lookup_flavoured_template_uses_get_order_status():
    """"Where is my order" (task 3.6's own framing) is exactly what a
    Customer Support template exists to answer — a template claiming that
    role without the one tool that answers the question would look
    finished and quietly not be."""
    assert any("get_order_status" in t.tools for t in TEMPLATES), (
        "no template offers order lookup — is there really a support-shaped one?"
    )


# --- the API surface ---------------------------------------------------------

async def test_the_templates_endpoint_needs_no_login():
    """Unauthenticated deliberately — the same catalogue for everyone, and
    someone should be able to see what a new bot could look like before
    signing up, the same way the voice catalogue already is."""
    from httpx import ASGITransport, AsyncClient

    from main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/bots/templates")
    assert resp.status_code == 200


async def test_the_templates_endpoint_returns_the_full_prompt_not_a_summary(client):
    """The picker pre-fills the actual create-bot form with this — a
    summary would mean a second fetch, or worse, a truncated prompt
    silently saved as the bot's real one."""
    resp = await client.get("/bots/templates")
    body = resp.json()
    assert len(body) == len(TEMPLATES)
    for entry, template in zip(body, TEMPLATES, strict=True):
        assert entry["system_prompt"] == template.system_prompt
        assert entry["tools"] == template.tools


async def test_creating_a_bot_from_a_templates_tools_gives_it_exactly_those(client, user_a_token):
    """The manual's step three and four in one motion: pick a template,
    land on an editable bot — but with the RIGHT tools already attached,
    not the fallback-to-everything a bot with nothing configured gets."""
    from app.services import tool_registry
    from tests.conftest import auth_headers

    template = next(t for t in TEMPLATES if len(t.tools) < len(_REAL_TOOL_NAMES))

    created = (await client.post(
        "/bots/", json={
            "name": "From template", "system_prompt": template.system_prompt,
            "voice_id": "a0e99841-438c-4a64-b679-ae501e7d6091", "llm_model": "gpt-4o-mini",
            "language": "en",
        },
        headers=auth_headers(user_a_token),
    )).json()

    for tool_name in template.tools:
        resp = await client.post(
            f"/bots/{created['id']}/tools/",
            json={
                "name": tool_name, "description": f"Template default: {tool_name}",
                "enabled": True, "long_running": False, "kind": "builtin", "builtin": tool_name,
                "method": "GET", "url": "", "headers": {}, "query": {}, "body": {},
                "parameters": [], "auth": {"kind": "none", "name": ""},
                "field_map": {}, "timeout_seconds": 8,
                "payment": {
                    "enabled": False, "reference_field": "", "amount_field": "", "link_field": "",
                    "signature_header": "X-Razorpay-Signature",
                    "webhook_reference_field": "payload.payment_link.entity.id",
                    "webhook_status_field": "payload.payment_link.entity.status",
                    "webhook_paid_value": "paid",
                },
            },
            headers=auth_headers(user_a_token),
        )
        assert resp.status_code == 201, resp.text

    tools, *_ = await tool_registry.load_tools_for_bot(created["id"])
    loaded_names = {getattr(t, "__name__", getattr(t, "name", "")) for t in tools}
    assert loaded_names == set(template.tools), (
        "the new bot did not get exactly the template's tools — "
        f"got {loaded_names}, wanted {set(template.tools)}"
    )
