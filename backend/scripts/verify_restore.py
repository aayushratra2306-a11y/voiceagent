"""Task 6.8 — check that a RESTORED database actually has what it should,
rather than trusting that a restore "completed" in Atlas's UI means the
data is really there.

The manual's own point is specific: "nearly everyone who assumes their
backups work discovers a problem the first time they actually try — a
missing permission, a wrong region, an incomplete snapshot." Those failures
all look identical from the Atlas console: a restore job that reports
success. The only way to actually know is to point something at the
result and check it holds real data, which is what this script is for.

Usage, after restoring a snapshot to a test cluster/deployment:

    python -m scripts.verify_restore --url "mongodb+srv://.../test-restore" --db voiceagent

This is deliberately NOT part of the automated test suite — it exists to
be run BY A PERSON, against a real restored database, as the actual
practice-restoring exercise task 6.8 asks for. What IS tested
automatically (tests/test_verify_restore.py) is this script's own logic:
that it correctly tells a healthy database from a broken one, using a
local database standing in for "the restore" — because the one thing this
repository can prove without an Atlas account is that the CHECK itself is
trustworthy. Whether a real Atlas restore succeeds is the part only a
person with that account can actually verify, which is the whole reason
the manual insists on doing it by hand rather than trusting a checkbox.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

# The complete list of collections this project's models create, kept as
# an explicit list rather than derived from `app.models.*` at runtime: this
# script has to run against a URL that may not be the URL running this
# process (a restored test cluster, not the live database), and it should
# not need this repo's Motor client and event loop already pointed at that
# target to enumerate what "complete" even means. Cross-checked against
# every model's `Settings.name` by tests/test_verify_restore.py, so this
# list drifting from the real models is caught by CI, not discovered
# during an actual incident.
EXPECTED_COLLECTIONS = [
    "users",
    "bots",
    "bot_tools",
    "documents",
    "orders",
    "appointments",
    "conversation_turns",
    "consent_records",
    "guardrail_incidents",
    "payment_sessions",
    "webhook_subscriptions",
    "webhook_deliveries",
    "webhook_outbox",
    "pending_approvals",
    "revoked_refresh_tokens",
]

# Task 2.10's uploaded knowledge-base files live in GridFS, which is two
# collections of its own rather than one Beanie Document — missed by the
# list above if not named explicitly, since GridFS is Motor's own
# mechanism, not a Beanie model with a `Settings.name`.
GRIDFS_COLLECTIONS = ["fs.files", "fs.chunks"]


@dataclass
class RestoreCheck:
    missing_collections: list[str] = field(default_factory=list)
    empty_collections: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    # GridFS is legitimately allowed to be empty — a fresh deployment with
    # no knowledge-base files uploaded yet has neither fs.files nor
    # fs.chunks, and that is a correct, healthy state, not a broken
    # restore. Tracked separately so it never triggers the same "empty
    # collection" warning a genuinely broken users/bots collection should.
    gridfs_present: bool = False

    @property
    def healthy(self) -> bool:
        return not self.missing_collections


async def check_database(motor_database) -> RestoreCheck:
    """The actual check, factored out from `main()` so a test can drive it
    directly against a real (if empty or partial) database without
    shelling out to a subprocess or faking a Motor client's whole API."""
    existing = set(await motor_database.list_collection_names())
    result = RestoreCheck()

    for name in EXPECTED_COLLECTIONS:
        if name not in existing:
            result.missing_collections.append(name)
            continue
        count = await motor_database[name].estimated_document_count()
        result.counts[name] = count
        if count == 0:
            result.empty_collections.append(name)

    result.gridfs_present = all(name in existing for name in GRIDFS_COLLECTIONS)

    return result


def _report(result: RestoreCheck, db_name: str) -> None:
    print(f"Checked database: {db_name}\n")

    if result.missing_collections:
        print("MISSING COLLECTIONS (this restore is incomplete):")
        for name in result.missing_collections:
            print(f"  - {name}")
        print()

    print("Document counts:")
    for name in EXPECTED_COLLECTIONS:
        if name in result.counts:
            flag = "  <- empty" if name in result.empty_collections else ""
            print(f"  {name:<28} {result.counts[name]:>8}{flag}")
    print()

    gridfs_status = "present" if result.gridfs_present else "absent — no files uploaded yet, or missing"
    print(f"GridFS (uploaded files): {gridfs_status}")
    print()

    if result.healthy:
        print("OK — every expected collection exists in this database.")
        if result.empty_collections:
            print(
                "Some collections are empty. On a fresh/test deployment that can be "
                "completely normal (e.g. a bot with no bookings yet) — an EMPTY "
                "collection is not proof of a broken restore the way a MISSING one "
                "is. Judge these against what you actually expected to be in the "
                "snapshot you restored."
            )
    else:
        print(
            "NOT OK — this does not look like a complete restore. Re-check the "
            "snapshot, the restore job's target, and Atlas's own restore log "
            "before trusting this database."
        )


async def _main(url: str, db_name: str) -> int:
    import motor.motor_asyncio

    client = motor.motor_asyncio.AsyncIOMotorClient(url, serverSelectionTimeoutMS=5000)
    try:
        # Fails fast and clearly rather than hanging on a bad connection
        # string — the exact "wrong region, missing permission" class of
        # mistake the manual's own tip warns is how a first real restore
        # attempt actually goes wrong.
        await client.admin.command("ping")
    except Exception as e:
        print(f"Could not reach {url!r}: {type(e).__name__}: {e}", file=sys.stderr)
        print("Check the connection string, network access list, and credentials "
              "on the restored cluster before re-running this.", file=sys.stderr)
        return 2

    result = await check_database(client[db_name])
    _report(result, db_name)
    return 0 if result.healthy else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="Connection string for the RESTORED database")
    ap.add_argument("--db", required=True, help="Database name to check")
    args = ap.parse_args()
    return asyncio.run(_main(args.url, args.db))


if __name__ == "__main__":
    raise SystemExit(main())
