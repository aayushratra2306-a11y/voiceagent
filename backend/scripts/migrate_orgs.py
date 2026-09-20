"""Task 5.1 — give every existing record an organisation.

    python -m scripts.migrate_orgs --dry-run   # what would change; changes nothing
    python -m scripts.migrate_orgs             # apply (after a db_backup snapshot)

Idempotent and resumable: only records still missing org_id are touched, so
a rerun after a crash, or after new code has been live for a while, finishes
the job and changes nothing else. Records whose bot or owner no longer
exists are listed and left untagged, which makes them invisible; nothing is
guessed. See Docs/specs/2026-09-19-organisations-teams-roles-design.md.

Fix round 1 (2026-09-20 review) — two things changed:

  - There was no production guard. A real run with no flags, from a shell
    where DB_NAME was never exported, resolves to app.core.config's
    fallback db_name = "voiceagent" — the live database, the same one a
    test run destroyed on 2026-09-13 (app/core/db_safety.py exists because
    of that). This mirrors the guard scripts/db_backup.py already uses for
    `restore`: refuse a real (non-dry-run) write against a database whose
    name doesn't declare itself disposable, unless the operator passes the
    exact same acknowledgement flag db_backup.py uses. A dry run needs no
    flag — it never writes, so there is nothing to guard.

  - A dry run used to under-report on a never-migrated database: step 1
    creates no personal orgs in dry-run mode, so step 2 found none and
    every bot-owned collection's cascade counted zero — on exactly the
    dataset the preview exists to describe. `_org_for_user` now simulates
    the organisation a real run would create for a user who doesn't have
    one yet (but does exist), so the dry-run counts approximate the real
    run instead of reading zero. Estimated counts are called out by name
    in the printed report (`estimated_users`), never silently presented as
    exact.

Fix round 2 (2026-09-20 review) — two more things changed:

  - `--dry-run` used to call `init_db(ALL_MODELS)` unconditionally. Beanie
    creates every declared index for every model it's given, at init time,
    whether or not a single document is ever read or written afterwards —
    so a dry run against a database that had never seen organisations or
    memberships created both those collections and eight new indexes
    before printing "DRY RUN — nothing changed", directly contradicting
    this module's own docstring. A dry run only ever reads through
    `database[...]` raw handles and `User.find_all()` / `User.get(...)`
    (see `_org_for_user`) — `ensure_personal_org` and
    `recount_owner_counts`, the only functions that touch Organisation or
    Membership through Beanie, are both called on the real-run path only
    (search this file for `if not dry_run` around them). So a dry run now
    initialises only `[User]`; a real run still initialises `ALL_MODELS`,
    since `ensure_personal_org`/`recount_owner_counts` need every model
    Beanie-registered.

  - The production guard checks the database NAME only, and cannot catch a
    right-name/wrong-cluster mistake (e.g. an operator's shell pointing
    `MONGODB_URL` at the wrong Atlas project while `DB_NAME` still reads
    "voiceagent"). The printed header now also names the connection HOST,
    with any embedded credentials stripped — see `_connection_host` — so
    that class of mistake is visible before a real run's guard is even
    reached.
"""

import argparse
import asyncio

from beanie import PydanticObjectId

from app.core.config import settings
from app.core.db_safety import is_disposable_database
from app.db.mongo import database, init_db
from app.models.registry import ALL_MODELS
from app.models.user import User
from app.services.orgs import ensure_personal_org, recount_owner_counts

MISSING = {"$or": [{"org_id": {"$exists": False}}, {"org_id": ""}, {"org_id": None}]}
BOT_OWNED = ("documents", "bot_tools", "conversation_turns", "appointments",
             "payment_sessions", "pending_approvals")


class ProductionGuardError(Exception):
    """A real run was refused: the database is not a disposable test/CI
    database, and the operator did not pass the acknowledgement flag."""


def _connection_host(url: str) -> str:
    """The host(s) this script will talk to, credentials stripped.

    The production guard (below) validates the DATABASE NAME only, so a
    right-name/wrong-cluster mistake — the shell's MONGODB_URL pointed at
    a different Atlas project while DB_NAME still happens to read
    "voiceagent" — sails straight through it. Printing the host next to
    the name at least puts that mismatch in front of whoever is running
    this. Never returns anything after the credentials boundary (the
    `user:pass@` part of the URL, if any).
    """
    rest = url.split("://", 1)[-1]
    rest = rest.rsplit("@", 1)[-1]  # drop "user:pass@" if present
    return rest.split("/", 1)[0].split("?", 1)[0]


async def _personal_org_of(user_id: str) -> str | None:
    org = await database["organisations"].find_one({"created_by": user_id, "personal": True})
    return str(org["_id"]) if org else None


async def _org_for_user(uid: str, dry_run: bool, simulated: dict[str, str]) -> str | None:
    """The organisation this user's records carry — or, in a dry run only,
    the one they WOULD carry once the real run creates it.

    A real personal organisation always wins. Failing that, a real run has
    nothing to guess (None: orphan, or not processed yet by this same
    call) — but a dry run on data that has never been migrated never
    actually creates that org (step 1 is read-only in dry-run mode), so
    every downstream count would otherwise read zero for a user who in
    fact has records waiting to be tagged. The placeholder string
    returned here is never written anywhere — a dry run never reaches a
    write — it exists only so `_tag`'s COUNT query has a truthy org to
    receive. A user_id with no matching User is a genuine orphan and is
    excluded from the estimate exactly as a real run would exclude it.
    """
    org = await _personal_org_of(uid)
    if org:
        return org
    if not dry_run:
        return None
    if uid in simulated:
        return simulated[uid]
    try:
        exists = await User.get(PydanticObjectId(uid)) is not None
    except Exception:
        exists = False
    if not exists:
        return None
    simulated[uid] = f"~estimated-org:{uid}"
    return simulated[uid]


async def _tag(coll: str, query: dict, org_id: str, dry_run: bool, report: dict, field: str = "org_id") -> None:
    q = {"$and": [query, MISSING if field == "org_id" else {f"{field}": {"$exists": False}}]}
    if dry_run:
        n = await database[coll].count_documents(q)
    else:
        n = (await database[coll].update_many(q, {"$set": {field: org_id}})).modified_count
    report[coll] = report.get(coll, 0) + n
    report["tagged_total"] += n


async def migrate(
    dry_run: bool,
    allow_non_disposable: bool = False,
    db_name: str | None = None,
) -> dict[str, int]:
    """`db_name` overrides only the SAFETY CHECK's idea of which database
    this is running against — it never opens a second connection and never
    changes what `database` (and therefore every read/write below) points
    at. It exists so tests can simulate a non-disposable name (e.g.
    db_name="voiceagent") while every actual read and write still goes to
    the real `database` handle — the one the test suite's conftest pins to
    voiceagent_test for the whole session.
    """
    name = db_name if db_name is not None else database.name
    print(f"[migrate_orgs] target database: {name!r} @ {_connection_host(settings.mongodb_url)} "
          f"({'dry run — no writes' if dry_run else 'APPLYING CHANGES'})")
    if not dry_run and not is_disposable_database(name) and not allow_non_disposable:
        raise ProductionGuardError(
            f"{name!r} does not look like a disposable test database (expected a name ending "
            f"in _test or _ci — see app/core/db_safety.py). Refusing to write. Take a snapshot "
            f"first (python -m scripts.db_backup snapshot), then rerun with "
            f"--i-understand-this-writes-to-a-non-test-database if this is really intended."
        )

    report: dict[str, int] = {"tagged_total": 0, "users_needing_org": 0}
    simulated: dict[str, str] = {}

    # 1. every user belongs somewhere
    for user in await User.find_all().to_list():
        has = await database["memberships"].find_one({"user_id": str(user.id)})
        if not has:
            report["users_needing_org"] += 1
            if not dry_run:
                await ensure_personal_org(user)

    # 2. bots and webhook subscriptions: their owner's personal organisation
    #    (or, dry-run only, the one that owner would get — see _org_for_user)
    for coll in ("bots", "webhook_subscriptions"):
        for uid in await database[coll].distinct("user_id", MISSING):
            org = await _org_for_user(uid, dry_run, simulated)
            if org:
                await _tag(coll, {"user_id": uid}, org, dry_run, report)

    # 3. everything bot-owned: its bot's organisation. A real run only looks
    #    at bots already tagged (by step 2 just above, or by an earlier
    #    partial run — org_id sticks once set); a dry run walks every bot
    #    and falls back to the owner's (possibly simulated) org, so the
    #    cascade counts approximate a first-ever real run instead of
    #    reading zero for every bot-owned collection.
    bot_orgs: dict[str, str] = {}
    bot_query = {} if dry_run else {"org_id": {"$nin": ["", None]}}
    async for bot in database["bots"].find(bot_query, {"org_id": 1, "user_id": 1}):
        b = str(bot["_id"])
        org = bot.get("org_id") or None
        if not org:
            if not dry_run:
                continue  # not yet tagged and this is a real run: leave it for a later pass
            org = await _org_for_user(bot.get("user_id", ""), dry_run, simulated)
        if not org:
            continue  # orphan: no such owner
        bot_orgs[b] = org
        for coll in BOT_OWNED:
            await _tag(coll, {"bot_id": b}, org, dry_run, report)
        await _tag("booking_slots", {"_id": {"$regex": f"^{b}\\|"}}, org, dry_run, report)
        await _tag("fs.files", {"metadata.bot_id": b}, org, dry_run, report, field="metadata.org_id")

    #    payment sessions and approvals whose bot_id is blank: their tool's
    #    organisation (or, dry-run only, that tool's own bot's estimated org,
    #    since a first-ever dry run never actually writes org_id onto the tool)
    for coll in ("payment_sessions", "pending_approvals"):
        for tool_id in await database[coll].distinct("tool_id", {"$and": [{"bot_id": {"$in": ["", None]}}, MISSING]}):
            try:
                tool = await database["bot_tools"].find_one({"_id": PydanticObjectId(tool_id)})
            except Exception:
                tool = None
            if not tool:
                continue
            org = tool.get("org_id") or (bot_orgs.get(tool.get("bot_id", "")) if dry_run else None)
            if org:
                await _tag(coll, {"tool_id": tool_id, "bot_id": {"$in": ["", None]}}, org, dry_run, report)

    # 4. owner counts match reality
    report["owner_counts_corrected"] = 0 if dry_run else await recount_owner_counts()

    # what is still untagged (expected: only the orphans the spec lists)
    for coll in ("bots", "webhook_subscriptions", *BOT_OWNED, "booking_slots"):
        report[f"untagged_{coll}"] = await database[coll].count_documents(MISSING)

    # how many of the counts above are estimates rather than exact — see
    # _org_for_user. Always 0 on a real run.
    report["estimated_users"] = len(simulated)
    return report


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--i-understand-this-writes-to-a-non-test-database", action="store_true",
        dest="allow_non_disposable",
        help="Required for a real (non-dry-run) migration against a database whose name does "
             "not end in _test or _ci. Take a scripts.db_backup snapshot first.",
    )
    args = parser.parse_args()
    # A dry run only ever reads: raw database[...] handles everywhere, plus
    # User.find_all()/User.get(...) in _org_for_user. ensure_personal_org and
    # recount_owner_counts — the only functions here that touch Organisation
    # or Membership through Beanie rather than a raw handle — are both
    # called on the real-run path only. So a dry run initialises just
    # [User]: enough for every model access it can actually reach, and none
    # of the index-creation side effects init_beanie(ALL_MODELS) would
    # otherwise cause against a database that has never seen them.
    await init_db([User] if args.dry_run else ALL_MODELS)
    try:
        report = await migrate(dry_run=args.dry_run, allow_non_disposable=args.allow_non_disposable)
    except ProductionGuardError as e:
        print(f"REFUSED: {e}")
        raise SystemExit(1) from e

    lines = [f"  {k}: {v}" for k, v in report.items()]
    if args.dry_run and report.get("estimated_users"):
        lines.append(
            f"  NOTE: counts above include ESTIMATES for {report['estimated_users']} user(s) "
            f"with no organisation yet — the real run creates one for each and tags their "
            f"records accordingly; these numbers approximate that, they are not exact."
        )
    print(("DRY RUN — nothing changed\n" if args.dry_run else "APPLIED\n") + "\n".join(lines))


if __name__ == "__main__":
    asyncio.run(_main())
