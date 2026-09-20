"""The migration: fills org_id everywhere it can, guesses nowhere, reruns safely."""

import pytest

from app.db.mongo import database
from app.models.organisation import Membership
from app.models.user import User
from scripts.migrate_orgs import MISSING, migrate

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _legacy_world():
    """Records exactly as pre-5.1 code wrote them: no org_id field at all."""
    user = User(email="legacy-mig@voiceagent-test.com", password_hash="x")
    await user.insert()
    uid = str(user.id)
    bot_id = (await database["bots"].insert_one({"user_id": uid, "name": "old bot"})).inserted_id
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


async def test_running_twice_is_a_no_op():
    await migrate(dry_run=False)
    report = await migrate(dry_run=False)
    assert report["tagged_total"] == 0
