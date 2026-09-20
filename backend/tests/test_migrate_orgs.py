"""The migration: fills org_id everywhere it can, guesses nowhere, reruns safely."""

import sys

import pytest

from app.db.mongo import database
from app.models.organisation import Membership, Organisation
from app.models.registry import ALL_MODELS
from app.models.user import User
from scripts import migrate_orgs
from scripts.migrate_orgs import MISSING, ProductionGuardError, migrate

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _legacy_world(tag: str = ""):
    """Records exactly as pre-5.1 code wrote them: no org_id field at all.

    `tag` keeps a second/third call's user email and bot name distinct
    from the original ("old bot" / legacy-mig@...) so the new guard tests
    (which must run unconditionally, unlike the conditional seeding the
    other tests share) don't collide with them or each other on User's
    unique email index.
    """
    user = User(email=f"legacy-mig{tag}@voiceagent-test.com", password_hash="x")
    await user.insert()
    uid = str(user.id)
    bot_id = (await database["bots"].insert_one({"user_id": uid, "name": f"old bot{tag}"})).inserted_id
    b = str(bot_id)
    await database["documents"].insert_one({"bot_id": b, "user_id": uid, "filename": "f.pdf"})
    await database["bot_tools"].insert_one({"bot_id": b, "name": "t", "description": "d"})
    tool_id = (await database["bot_tools"].find_one({"bot_id": b}))["_id"]
    await database["conversation_turns"].insert_one({"bot_id": b, "session_id": "s"})
    await database["conversation_turns"].insert_one({"bot_id": None, "session_id": "orphan"})
    await database["appointments"].insert_one({"bot_id": b, "reference": "AAAA"})
    await database["booking_slots"].insert_one({"_id": f"{b}|2026-10-01|10:00"})
    await database["payment_sessions"].insert_one({"bot_id": "", "tool_id": str(tool_id), "reference": "R"})
    await database["pending_approvals"].insert_one({"bot_id": b, "tool_id": str(tool_id), "user_id": uid})
    await database["webhook_subscriptions"].insert_one({"user_id": uid, "event": "call.ended", "url": "u"})
    await database["fs.files"].insert_one({"filename": "f.pdf", "metadata": {"bot_id": b}})
    return uid, b


async def test_dry_run_changes_nothing():
    uid, _ = await _legacy_world()
    report = await migrate(dry_run=True)
    assert report["users_needing_org"] >= 1
    assert await database["bots"].count_documents({"user_id": uid, **MISSING}) == 1
    assert await Membership.find(Membership.user_id == uid).count() == 0
    # Fix round 1: on a never-migrated dataset the dry run must not report
    # zero for this user's cascade just because step 1 didn't actually
    # create their personal org yet — it estimates it instead.
    assert report["estimated_users"] >= 1
    assert report.get("documents", 0) >= 1


async def test_a_real_run_refuses_a_non_disposable_database_without_the_flag():
    """Same guard scripts/db_backup.py already uses for `restore`: refuse a
    real write against a database name that doesn't declare itself
    disposable, unless the operator passes the acknowledgement flag.

    The simulated name never changes which database is actually touched —
    `db_name` only feeds the safety check; every read/write here still
    goes to the real (test) `database` handle.
    """
    uid, _ = await _legacy_world(tag="-guard-refuse")
    with pytest.raises(ProductionGuardError):
        await migrate(dry_run=False, db_name="voiceagent")
    assert await database["bots"].count_documents({"user_id": uid, **MISSING}) == 1


async def test_a_real_run_proceeds_on_a_non_disposable_database_with_the_flag():
    uid, _ = await _legacy_world(tag="-guard-allow")
    await migrate(dry_run=False, db_name="voiceagent", allow_non_disposable=True)
    assert await database["bots"].count_documents({"user_id": uid, **MISSING}) == 0


async def test_a_real_run_tags_everything_it_can_and_nothing_it_cannot():
    uid, b = await _legacy_world() if not await database["bots"].find_one({"name": "old bot"}) else (None, None)
    await migrate(dry_run=False)
    bot = await database["bots"].find_one({"name": "old bot"})
    org = bot["org_id"]
    assert org
    for coll in ("documents", "bot_tools", "appointments", "pending_approvals"):
        assert await database[coll].count_documents({"bot_id": str(bot["_id"]), **MISSING}) == 0, coll
    assert (await database["payment_sessions"].find_one({"reference": "R"}))["org_id"] == org  # via the tool
    assert (await database["booking_slots"].find_one({"_id": {"$regex": f"^{bot['_id']}\\|"}}))["org_id"] == org
    assert (await database["fs.files"].find_one({"metadata.bot_id": str(bot["_id"])}))["metadata"]["org_id"] == org
    assert (await database["webhook_subscriptions"].find_one({"url": "u"}))["org_id"] == org
    # The orphan turn is left untagged, not guessed.
    assert (await database["conversation_turns"].find_one({"session_id": "orphan"})).get("org_id") in (None, "")


async def test_main_dry_run_initialises_only_the_user_model(monkeypatch):
    """Blocker 1 (2026-09-20 review): `_main()` used to call
    `init_db(ALL_MODELS)` unconditionally, dry run or not. Beanie creates
    every declared index for every model it's given at init time — so a
    dry run against a database that had never seen organisations or
    memberships created both collections and their indexes before ever
    printing "DRY RUN — nothing changed".

    A dry run only ever reads: raw `database[...]` handles everywhere, plus
    `User.find_all()` / `User.get(...)` in `_org_for_user`. `ensure_personal_org`
    and `recount_owner_counts` — the only functions in this module that touch
    Organisation or Membership through Beanie rather than a raw handle — are
    both called on the real-run path only (see the `if not dry_run:` guards
    around each call site in `migrate()`). So `_main()` must initialise just
    `[User]` for `--dry-run`.
    """
    seen: list[list] = []

    async def fake_init_db(models):
        seen.append(list(models))

    monkeypatch.setattr(migrate_orgs, "init_db", fake_init_db)
    monkeypatch.setattr(sys, "argv", ["migrate_orgs", "--dry-run"])
    await migrate_orgs._main()
    assert seen == [[User]]


async def test_main_real_run_still_initialises_every_model(monkeypatch):
    """The real-run path still needs every Beanie model: `ensure_personal_org`
    and `recount_owner_counts` run only when dry_run is False, and both touch
    Organisation/Membership through Beanie."""
    seen: list[list] = []

    async def fake_init_db(models):
        seen.append(list(models))

    monkeypatch.setattr(migrate_orgs, "init_db", fake_init_db)
    monkeypatch.setattr(sys, "argv", ["migrate_orgs"])
    await migrate_orgs._main()
    assert seen == [ALL_MODELS]


async def test_dry_run_does_not_recreate_a_dropped_organisation_index(monkeypatch):
    """The direct proof behind blocker 1, at the exact granularity the
    review named: "Beanie's init creates every declared index... eight new
    indexes... on a 512 MB Atlas M0." If `_main()` still called
    `init_db(ALL_MODELS)` in dry-run mode, registering Organisation would
    recreate this index as a side effect — the same mechanism that would
    create the whole collection from nothing on a never-migrated database.

    Can't reproduce "the collection doesn't exist at all" against
    `voiceagent_test` itself: it's the shared, session-wide database
    (conftest's autouse `init_db(ALL_MODELS)` created both organisations
    and memberships before this file ever ran), and the Atlas user this
    project connects with locally has no access to any other database
    name to prove the from-scratch version safely. Dropping one of
    Organisation's own indexes and checking it stays gone through a dry
    run is the same claim, provably, on the one database this suite may
    touch — and it's restored in `finally` either way.
    """
    orgs = Organisation.get_motor_collection()
    index_name = "one_personal_org_per_user"
    before = await orgs.index_information()
    assert index_name in before  # sanity: it's really there beforehand

    await orgs.drop_index(index_name)
    try:
        assert index_name not in await orgs.index_information()

        monkeypatch.setattr(sys, "argv", ["migrate_orgs", "--dry-run"])
        await migrate_orgs._main()

        assert index_name not in await orgs.index_information(), (
            "the dry run recreated an Organisation index — it must call "
            "init_db([User]) only, not ALL_MODELS, while dry_run is True"
        )
    finally:
        from beanie import init_beanie

        await init_beanie(database=database, document_models=ALL_MODELS)
        assert index_name in await orgs.index_information()  # left as found


async def test_running_twice_is_a_no_op():
    await migrate(dry_run=False)

    users = await User.find_all().to_list()
    org_counts_before = {}
    membership_counts_before = {}
    for user in users:
        uid = str(user.id)
        org_counts_before[uid] = await database["organisations"].count_documents(
            {"created_by": uid, "personal": True}
        )
        membership_counts_before[uid] = await database["memberships"].count_documents({"user_id": uid})
        # Never more than one personal org per user (some users in this
        # shared test database only ever got invited into someone else's
        # org and so legitimately have zero of their own), and every user
        # belongs somewhere after a real run — the baseline the second run
        # must not disturb.
        assert org_counts_before[uid] <= 1, uid
        assert membership_counts_before[uid] >= 1, uid

    report = await migrate(dry_run=False)

    assert report["tagged_total"] == 0
    for user in users:
        uid = str(user.id)
        # No duplicate organisation and no duplicate membership for any
        # user: the second run's counts must match the first run's exactly.
        assert await database["organisations"].count_documents(
            {"created_by": uid, "personal": True}
        ) == org_counts_before[uid], uid
        assert await database["memberships"].count_documents(
            {"user_id": uid}
        ) == membership_counts_before[uid], uid
