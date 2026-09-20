"""Task 5.1 — give every existing record an organisation.

    python -m scripts.migrate_orgs --dry-run   # what would change; changes nothing
    python -m scripts.migrate_orgs             # apply (after a db_backup snapshot)

Idempotent and resumable: only records still missing org_id are touched, so
a rerun after a crash, or after new code has been live for a while, finishes
the job and changes nothing else. Records whose bot or owner no longer
exists are listed and left untagged, which makes them invisible; nothing is
guessed. See Docs/specs/2026-09-19-organisations-teams-roles-design.md.
"""

import argparse
import asyncio

from beanie import PydanticObjectId

from app.db.mongo import database, init_db
from app.models.registry import ALL_MODELS
from app.models.user import User
from app.services.orgs import ensure_personal_org, recount_owner_counts

MISSING = {"$or": [{"org_id": {"$exists": False}}, {"org_id": ""}, {"org_id": None}]}
BOT_OWNED = ("documents", "bot_tools", "conversation_turns", "appointments",
             "payment_sessions", "pending_approvals")


async def _personal_org_of(user_id: str) -> str | None:
    org = await database["organisations"].find_one({"created_by": user_id, "personal": True})
    return str(org["_id"]) if org else None


async def _tag(coll: str, query: dict, org_id: str, dry_run: bool, report: dict, field: str = "org_id") -> None:
    q = {"$and": [query, MISSING if field == "org_id" else {f"{field}": {"$exists": False}}]}
    if dry_run:
        n = await database[coll].count_documents(q)
    else:
        n = (await database[coll].update_many(q, {"$set": {field: org_id}})).modified_count
    report[coll] = report.get(coll, 0) + n
    report["tagged_total"] += n


async def migrate(dry_run: bool) -> dict[str, int]:
    report: dict[str, int] = {"tagged_total": 0, "users_needing_org": 0}

    # 1. every user belongs somewhere
    for user in await User.find_all().to_list():
        has = await database["memberships"].find_one({"user_id": str(user.id)})
        if not has:
            report["users_needing_org"] += 1
            if not dry_run:
                await ensure_personal_org(user)

    # 2. bots and webhook subscriptions: their owner's personal organisation
    for coll in ("bots", "webhook_subscriptions"):
        for uid in await database[coll].distinct("user_id", MISSING):
            org = await _personal_org_of(uid)
            if org:
                await _tag(coll, {"user_id": uid}, org, dry_run, report)

    # 3. everything bot-owned: its bot's organisation
    async for bot in database["bots"].find({"org_id": {"$nin": ["", None]}}, {"org_id": 1}):
        b, org = str(bot["_id"]), bot["org_id"]
        for coll in BOT_OWNED:
            await _tag(coll, {"bot_id": b}, org, dry_run, report)
        await _tag("booking_slots", {"_id": {"$regex": f"^{b}\\|"}}, org, dry_run, report)
        await _tag("fs.files", {"metadata.bot_id": b}, org, dry_run, report, field="metadata.org_id")

    #    payment sessions and approvals whose bot_id is blank: their tool's organisation
    for coll in ("payment_sessions", "pending_approvals"):
        for tool_id in await database[coll].distinct("tool_id", {"$and": [{"bot_id": {"$in": ["", None]}}, MISSING]}):
            try:
                tool = await database["bot_tools"].find_one({"_id": PydanticObjectId(tool_id)})
            except Exception:
                tool = None
            if tool and tool.get("org_id"):
                await _tag(coll, {"tool_id": tool_id, "bot_id": {"$in": ["", None]}}, tool["org_id"], dry_run, report)

    # 4. owner counts match reality
    report["owner_counts_corrected"] = 0 if dry_run else await recount_owner_counts()

    # what is still untagged (expected: only the orphans the spec lists)
    for coll in ("bots", "webhook_subscriptions", *BOT_OWNED, "booking_slots"):
        report[f"untagged_{coll}"] = await database[coll].count_documents(MISSING)
    return report


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    await init_db(ALL_MODELS)
    report = await migrate(dry_run=args.dry_run)
    print(("DRY RUN — nothing changed\n" if args.dry_run else "APPLIED\n")
          + "\n".join(f"  {k}: {v}" for k, v in report.items()))


if __name__ == "__main__":
    asyncio.run(_main())
