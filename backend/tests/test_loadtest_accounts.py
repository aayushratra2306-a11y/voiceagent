"""Task 4.8 — the disposable accounts a load test calls through.

The load test needs one account per simultaneous call, because the server
gives each account exactly one live call and hangs up the previous one when
the same account starts another (connect.py, _end_previous_calls_for).

Almost all of this file is about the DELETE side. Creating accounts is
harmless; deleting them runs against the production database and is matched
by a pattern, and a pattern that is one character too broad deletes real
customers. So the pattern is tested first and tested hardest — including the
addresses that merely look like test ones.
"""

import json

from aiohttp import web

from scripts.loadtest_accounts import (
    LOADTEST_DOMAIN,
    account_email,
    create,
    is_loadtest_account,
    loadtest_targets,
)


def test_every_address_it_creates_is_one_it_will_recognise_later():
    """The property that actually matters: create and delete must agree. If
    these two ever drift apart, the test accounts become permanent."""
    for i in range(10):
        assert is_loadtest_account(account_email("abc123", i))


def test_each_call_gets_its_own_address():
    emails = {account_email("abc123", i) for i in range(10)}

    assert len(emails) == 10


def test_a_real_customer_address_is_never_matched():
    assert not is_loadtest_account("aayush.ratra2306@gmail.com")
    assert not is_loadtest_account("someone@voiceagent.com")


def test_an_address_that_only_looks_like_a_test_one_is_not_matched():
    """Each of these contains the test domain somewhere and belongs to
    nobody's load test. A `in` check instead of a suffix check would delete
    all three."""
    assert not is_loadtest_account(f"user@not{LOADTEST_DOMAIN}")
    assert not is_loadtest_account(f"{LOADTEST_DOMAIN}@gmail.com")
    assert not is_loadtest_account(f"user@sub.{LOADTEST_DOMAIN}")
    assert not is_loadtest_account(f"user@{LOADTEST_DOMAIN}.co")


def test_the_match_ignores_capitalisation():
    """Email domains are case-insensitive, and an address stored with a
    capital letter would otherwise survive every cleanup forever."""
    assert is_loadtest_account(f"LT-ABC-1@{LOADTEST_DOMAIN.upper()}")


def test_an_empty_or_malformed_address_is_not_matched():
    assert not is_loadtest_account("")
    assert not is_loadtest_account("@")
    assert not is_loadtest_account(LOADTEST_DOMAIN)


def test_the_test_domain_is_one_that_can_never_be_a_real_address():
    """example.com is reserved by RFC 2606 for exactly this: it cannot be
    registered, so no real person can ever hold an address in it."""
    assert LOADTEST_DOMAIN.endswith("example.com")


# ---------------------------------------------------------------------------
# Choosing what to delete
# ---------------------------------------------------------------------------
#
# This is the dangerous half, so it is a plain function over lists that were
# already fetched: what gets deleted can be decided, and checked, without a
# database anywhere near it.


class _Row:
    """Stands in for a Beanie document — only the fields the choice reads,
    across the four kinds of row involved (User, Bot, Membership,
    Organisation)."""

    def __init__(
        self,
        id_: str,
        email: str | None = None,
        user_id: str | None = None,
        org_id: str | None = None,
        created_by: str | None = None,
    ):
        self.id = id_
        self.email = email
        self.user_id = user_id
        self.org_id = org_id
        self.created_by = created_by


def test_a_load_test_users_bots_are_chosen():
    users = [_Row("u1", email=account_email("abc", 0))]
    bots = [_Row("b1", user_id="u1")]

    chosen = loadtest_targets(users, bots, [], [])

    assert chosen.user_ids == ["u1"]
    assert chosen.bot_ids == ["b1"]


def test_a_real_customers_bots_are_never_chosen():
    users = [_Row("u1", email="someone@gmail.com")]
    bots = [_Row("b1", user_id="u1")]

    chosen = loadtest_targets(users, bots, [], [])

    assert chosen.user_ids == []
    assert chosen.bot_ids == []


def test_only_the_load_test_half_is_chosen_when_both_exist():
    """The realistic case: production holds both at once."""
    users = [
        _Row("u1", email=account_email("abc", 0)),
        _Row("u2", email="someone@gmail.com"),
    ]
    bots = [_Row("b1", user_id="u1"), _Row("b2", user_id="u2")]

    chosen = loadtest_targets(users, bots, [], [])

    assert chosen.user_ids == ["u1"]
    assert chosen.bot_ids == ["b1"]


def test_the_bot_ids_are_kept_so_transcripts_can_be_found():
    """Every simulated turn writes a ConversationTurn — the recorder is real
    even in rehearsal mode. Those rows are reachable only by bot_id, so the
    ids have to be collected BEFORE the bots are deleted or the transcripts
    are stranded in production with nothing tying them to a test account."""
    users = [_Row("u1", email=account_email("abc", 0))]
    bots = [_Row("b1", user_id="u1"), _Row("b2", user_id="u1")]

    chosen = loadtest_targets(users, bots, [], [])

    assert sorted(chosen.bot_ids) == ["b1", "b2"]


# ---------------------------------------------------------------------------
# Task 5.1 — memberships and the organisations a load-test account created
# ---------------------------------------------------------------------------
#
# create() now puts every load-test bot in the account's personal
# organisation (see the fake-server tests below), so a clean delete has to
# take the membership and, if nothing else is left in it, the organisation
# too — or every load test leaves an abandoned org behind forever.


def test_a_load_test_users_membership_is_chosen():
    users = [_Row("u1", email=account_email("abc", 0))]
    memberships = [_Row("m1", user_id="u1", org_id="org-1")]

    chosen = loadtest_targets(users, [], memberships, [])

    assert chosen.membership_ids == ["m1"]


def test_a_real_users_membership_is_never_chosen():
    users = [_Row("u1", email="someone@gmail.com")]
    memberships = [_Row("m1", user_id="u1", org_id="org-1")]

    chosen = loadtest_targets(users, [], memberships, [])

    assert chosen.membership_ids == []


def test_an_organisation_the_load_test_user_created_alone_is_removed():
    """Its own personal org, created by create(): once the one membership
    in it is gone, nothing else is holding it open."""
    users = [_Row("u1", email=account_email("abc", 0))]
    memberships = [_Row("m1", user_id="u1", org_id="org-1")]
    orgs = [_Row("org-1", created_by="u1")]

    chosen = loadtest_targets(users, [], memberships, orgs)

    assert chosen.org_ids == ["org-1"]


def test_an_organisation_still_shared_with_someone_else_is_kept():
    """A load-test user who was invited into a real organisation must not
    take it down with them when they're cleaned up."""
    users = [_Row("u1", email=account_email("abc", 0))]
    memberships = [
        _Row("m1", user_id="u1", org_id="org-1"),
        _Row("m2", user_id="real-user", org_id="org-1"),
    ]
    orgs = [_Row("org-1", created_by="u1")]

    chosen = loadtest_targets(users, [], memberships, orgs)

    assert chosen.membership_ids == ["m1"]
    assert chosen.org_ids == []


def test_an_organisation_not_created_by_a_load_test_user_is_never_removed():
    """Only organisations the load-test user themself created are candidates
    for removal — never someone else's, even if their last member happened
    to be a load-test account leaving it."""
    users = [_Row("u1", email=account_email("abc", 0))]
    memberships = [_Row("m1", user_id="u1", org_id="org-1")]
    orgs = [_Row("org-1", created_by="a-real-customer")]

    chosen = loadtest_targets(users, [], memberships, orgs)

    assert chosen.org_ids == []


# ---------------------------------------------------------------------------
# create() over a fake server — GET /orgs, then X-Org-Id on POST /bots/
# ---------------------------------------------------------------------------


async def _run_fake_signup_server(port, orgs_body, monkeypatch, tmp_path):
    """Registers, logs in, reads /orgs and creates one bot, all against a
    local fake server, and returns the headers POST /bots/ was sent."""
    monkeypatch.setenv("LOADTEST_PASSWORD", "x" * 12)
    captured: dict = {}

    async def register(request):
        return web.json_response({}, status=201)

    async def login(request):
        return web.json_response({"access_token": "tok"})

    async def orgs(request):
        captured["orgs_auth"] = request.headers.get("Authorization")
        return web.json_response(orgs_body)

    async def create_bot(request):
        captured["bots_headers"] = dict(request.headers)
        return web.json_response({"id": "bot-1"}, status=201)

    app = web.Application()
    app.router.add_post("/auth/register", register)
    app.router.add_post("/auth/login", login)
    app.router.add_get("/orgs", orgs)
    app.router.add_post("/bots/", create_bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        out = tmp_path / "accounts.json"
        await create(f"http://127.0.0.1:{port}", 1, out)
        return captured, json.loads(out.read_text(encoding="utf-8"))
    finally:
        await runner.cleanup()


def test_create_sends_the_personal_orgs_id_when_creating_the_bot(unused_tcp_port, tmp_path, monkeypatch):
    import asyncio

    captured, written = asyncio.run(_run_fake_signup_server(
        unused_tcp_port,
        [{"id": "org-1", "name": "w", "personal": True, "role": "owner"}],
        monkeypatch, tmp_path,
    ))

    assert captured["orgs_auth"] == "Bearer tok"
    assert captured["bots_headers"]["X-Org-Id"] == "org-1"
    assert written == [{"email": written[0]["email"], "bot_id": "bot-1", "org_id": "org-1"}]


def test_create_picks_the_personal_org_even_when_it_is_not_first(unused_tcp_port, tmp_path, monkeypatch):
    """GET /orgs can return several organisations (an invited account joins
    more than its own); the PERSONAL one, not the first one, is where the
    load-test bot belongs."""
    import asyncio

    captured, written = asyncio.run(_run_fake_signup_server(
        unused_tcp_port,
        [
            {"id": "org-0", "name": "shared", "personal": False, "role": "member"},
            {"id": "org-1", "name": "w", "personal": True, "role": "owner"},
        ],
        monkeypatch, tmp_path,
    ))

    assert captured["bots_headers"]["X-Org-Id"] == "org-1"
    assert written[0]["org_id"] == "org-1"
