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

from scripts.loadtest_accounts import (
    LOADTEST_DOMAIN,
    account_email,
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
    """Stands in for a Beanie document — only the two fields the choice reads."""

    def __init__(self, id_: str, email: str | None = None, user_id: str | None = None):
        self.id = id_
        self.email = email
        self.user_id = user_id


def test_a_load_test_users_bots_are_chosen():
    users = [_Row("u1", email=account_email("abc", 0))]
    bots = [_Row("b1", user_id="u1")]

    chosen = loadtest_targets(users, bots)

    assert chosen.user_ids == ["u1"]
    assert chosen.bot_ids == ["b1"]


def test_a_real_customers_bots_are_never_chosen():
    users = [_Row("u1", email="someone@gmail.com")]
    bots = [_Row("b1", user_id="u1")]

    chosen = loadtest_targets(users, bots)

    assert chosen.user_ids == []
    assert chosen.bot_ids == []


def test_only_the_load_test_half_is_chosen_when_both_exist():
    """The realistic case: production holds both at once."""
    users = [
        _Row("u1", email=account_email("abc", 0)),
        _Row("u2", email="someone@gmail.com"),
    ]
    bots = [_Row("b1", user_id="u1"), _Row("b2", user_id="u2")]

    chosen = loadtest_targets(users, bots)

    assert chosen.user_ids == ["u1"]
    assert chosen.bot_ids == ["b1"]


def test_the_bot_ids_are_kept_so_transcripts_can_be_found():
    """Every simulated turn writes a ConversationTurn — the recorder is real
    even in rehearsal mode. Those rows are reachable only by bot_id, so the
    ids have to be collected BEFORE the bots are deleted or the transcripts
    are stranded in production with nothing tying them to a test account."""
    users = [_Row("u1", email=account_email("abc", 0))]
    bots = [_Row("b1", user_id="u1"), _Row("b2", user_id="u1")]

    chosen = loadtest_targets(users, bots)

    assert sorted(chosen.bot_ids) == ["b1", "b2"]
