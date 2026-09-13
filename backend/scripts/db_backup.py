"""Database backups: snapshot, verify, retain, restore.

Written after 2026-09-13, when the production database was dropped by a test
run on the live server and Atlas had no backup to restore from. This is
backup LAYER 1: automatic snapshots on the server itself, independent of
Atlas. (Layer 2 copies them off the server; see deploy/pull-backups.ps1.)

Snapshot layout, compatible with `mongorestore --gzip <dir>`:

    <root>/20260913T120000Z/
        manifest.json               counts per collection, written last
        <collection>.bson.gz        every document, raw BSON
        <collection>.metadata.json  index definitions

Usage (inside the backend image, where settings point at the database):

    python -m scripts.db_backup snapshot           take one now
    python -m scripts.db_backup list               what exists, with counts
    python -m scripts.db_backup verify <snapshot>  prove a snapshot is readable and complete
    python -m scripts.db_backup loop               what the `backup` compose service runs
    python -m scripts.db_backup restore <snapshot> --target-db <name>

What each command can change:
    snapshot/loop   reads the database; writes only under the backup folder
                    (loop also deletes OLD snapshot folders, see plan_deletions)
    list/verify     read-only
    restore         INSERTS into --target-db; never deletes or overwrites.
                    Refuses a non-test database name unless
                    --i-understand-this-writes-to-a-non-test-database is given.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import bson
from bson import json_util
from loguru import logger
from pymongo.errors import BulkWriteError

from app.core.db_safety import is_disposable_database

SNAPSHOT_RE = re.compile(r"^\d{8}T\d{6}Z$")
FREEZE_MARKER = "RETENTION_FROZEN"

KEEP_EVERYTHING_FOR = dt.timedelta(days=7)
KEEP_DAILY_FOR = dt.timedelta(days=30)
ALWAYS_KEEP_NEWEST = 4


class RestoreRefused(Exception):
    """A restore was stopped before writing anything."""


# --- snapshot -----------------------------------------------------------------------------

def snapshot_name(now: dt.datetime) -> str:
    return now.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def write_snapshot(db, root: Path, now: dt.datetime) -> Path:
    """Write every collection to a new snapshot folder and return it.

    Built in a `.partial` folder and renamed into place only once the
    manifest is written, so an interrupted backup can never be mistaken for
    a finished one by retention or by a restore.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    final = root / snapshot_name(now)
    partial = root / f"{final.name}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir()

    try:
        counts: dict[str, int] = {}
        for name in sorted(db.list_collection_names()):
            if name.startswith("system."):
                continue
            collection = db[name]
            count = 0
            with gzip.open(partial / f"{name}.bson.gz", "wb") as out:
                for batch in collection.find_raw_batches():
                    out.write(batch)
                    count += len(bson.decode_all(batch))
            indexes = []
            for index_name, info in collection.index_information().items():
                entry = {k: v for k, v in info.items() if k not in ("v", "ns", "key")}
                entry["name"] = index_name
                entry["key"] = [[field, direction] for field, direction in info["key"]]
                indexes.append(entry)
            (partial / f"{name}.metadata.json").write_text(
                json_util.dumps({"indexes": indexes}), encoding="utf-8")
            counts[name] = count

        manifest = {
            "format": 1,
            "database": db.name,
            "created_at": now.astimezone(dt.UTC).isoformat(),
            "collections": counts,
            "total_documents": sum(counts.values()),
        }
        (partial / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.replace(partial, final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return final


def read_manifest(snapshot: Path) -> dict:
    return json.loads((Path(snapshot) / "manifest.json").read_text(encoding="utf-8"))


def read_collection(snapshot: Path, name: str) -> list[dict]:
    data = gzip.decompress((Path(snapshot) / f"{name}.bson.gz").read_bytes())
    return bson.decode_all(data)


def verify_snapshot(snapshot: Path) -> list[str]:
    """Every problem found, or [] if every collection decodes and matches
    its manifest count. A backup nobody has checked is a hope, not a backup."""
    snapshot = Path(snapshot)
    try:
        manifest = read_manifest(snapshot)
    except Exception as e:
        return [f"manifest unreadable: {type(e).__name__}: {e}"]

    problems = []
    for name, expected in manifest["collections"].items():
        try:
            actual = len(read_collection(snapshot, name))
        except FileNotFoundError:
            problems.append(f"{name}: file missing")
            continue
        except Exception as e:
            problems.append(f"{name}: unreadable ({type(e).__name__}: {e})")
            continue
        if actual != expected:
            problems.append(f"{name}: manifest says {expected} documents, file has {actual}")
    return problems


def list_snapshots(root: Path) -> list[Path]:
    """Finished snapshots, oldest first."""
    root = Path(root)
    if not root.exists():
        return []
    return sorted(
        p for p in root.iterdir()
        if p.is_dir() and SNAPSHOT_RE.match(p.name) and (p / "manifest.json").exists()
    )


# --- noticing the data itself vanished ----------------------------------------------------

def detect_shrink(previous_total: int | None, current_total: int) -> str | None:
    """A reason to raise the alarm, or None.

    Backups keep running after data is wiped, and every snapshot of the
    empty database is newer than the good ones. Unnoticed, retention would
    rotate the good ones away. This is what notices.
    """
    if previous_total is None:
        return None
    if previous_total > 0 and current_total == 0:
        return f"database went from {previous_total} documents to EMPTY"
    if previous_total >= 10 and current_total < previous_total * 0.5:
        return f"document count fell from {previous_total} to {current_total}"
    return None


# --- retention ----------------------------------------------------------------------------

def plan_deletions(names: list[str], now: dt.datetime, *, frozen: bool) -> list[str]:
    """Which snapshot folders to delete. Pure: decides, deletes nothing.

    Keeps: everything from the last 7 days; the newest snapshot of each day
    for 30 days; and the newest 4 regardless of age, so a server whose
    backups stopped for weeks does not delete its last copies when they
    restart. Deletes nothing at all while frozen.
    """
    if frozen:
        return []
    dated = []
    for name in names:
        if SNAPSHOT_RE.match(name):
            taken = dt.datetime.strptime(name, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
            dated.append((taken, name))
    dated.sort(reverse=True)

    keep = {name for _taken, name in dated[:ALWAYS_KEEP_NEWEST]}
    seen_days = set()
    for taken, name in dated:
        age = now - taken
        if age <= KEEP_EVERYTHING_FOR:
            keep.add(name)
        if age <= KEEP_DAILY_FOR and taken.date() not in seen_days:
            seen_days.add(taken.date())
            keep.add(name)
    return sorted(name for _taken, name in dated if name not in keep)


def _delete_snapshot(root: Path, name: str) -> None:
    target = (Path(root) / name).resolve()
    if not SNAPSHOT_RE.match(name) or target.parent != Path(root).resolve():
        raise ValueError(f"refusing to delete {target}")
    shutil.rmtree(target)


def run_once(db, root: Path, now: dt.datetime) -> dict:
    """One backup cycle: snapshot, verify, check for data loss, retention."""
    root = Path(root)
    existing = list_snapshots(root)
    previous_total = read_manifest(existing[-1])["total_documents"] if existing else None

    snapshot = write_snapshot(db, root, now)
    problems = verify_snapshot(snapshot)
    total = read_manifest(snapshot)["total_documents"]

    alarm = detect_shrink(previous_total, total)
    if alarm:
        with open(root / FREEZE_MARKER, "a", encoding="utf-8") as marker:
            marker.write(f"{now.isoformat()}  {alarm}  (snapshot {snapshot.name})\n")
        logger.error(
            f"[BACKUP] {alarm}. Retention is FROZEN, so no snapshot will be deleted. Check the "
            f"data, then delete {root / FREEZE_MARKER} to resume normal retention."
        )

    deleted = []
    if problems:
        logger.error(f"[BACKUP] Snapshot {snapshot.name} failed verification: {problems}")
    else:
        frozen = (root / FREEZE_MARKER).exists()
        for name in plan_deletions([p.name for p in list_snapshots(root)], now, frozen=frozen):
            _delete_snapshot(root, name)
            deleted.append(name)

    report = {
        "snapshot": snapshot.name,
        "total_documents": total,
        "problems": problems,
        "shrink_alarm": alarm,
        "deleted": deleted,
    }
    logger.info(f"[BACKUP] {report}")
    return report


# --- restore ------------------------------------------------------------------------------

def restore_snapshot(snapshot: Path, client, target_db: str, *,
                     allow_non_disposable: bool = False,
                     allow_non_empty: bool = False) -> dict:
    """Insert a snapshot's documents into target_db. Never deletes or overwrites."""
    snapshot = Path(snapshot)
    problems = verify_snapshot(snapshot)
    if problems:
        raise RestoreRefused(f"snapshot failed verification: {problems}")
    if not is_disposable_database(target_db) and not allow_non_disposable:
        raise RestoreRefused(
            f"{target_db!r} is not a test database name. Restoring into it writes to a real "
            f"database; pass allow_non_disposable only after taking a snapshot of it first."
        )

    manifest = read_manifest(snapshot)
    db = client[target_db]
    if not allow_non_empty:
        occupied = [name for name in manifest["collections"] if db[name].count_documents({}) > 0]
        if occupied:
            raise RestoreRefused(f"target {target_db!r} is not empty: {occupied}")

    report = {}
    for name in manifest["collections"]:
        docs = read_collection(snapshot, name)
        inserted = skipped = 0
        if docs:
            try:
                db[name].insert_many(docs, ordered=False)
                inserted = len(docs)
            except BulkWriteError as e:
                errors = e.details.get("writeErrors", [])
                if any(err.get("code") != 11000 for err in errors):
                    raise
                skipped = len(errors)
                inserted = e.details.get("nInserted", len(docs) - skipped)

        metadata = json_util.loads((snapshot / f"{name}.metadata.json").read_text(encoding="utf-8"))
        for index in metadata["indexes"]:
            if index["name"] == "_id_":
                continue
            options = {k: v for k, v in index.items() if k != "key"}
            try:
                db[name].create_index([tuple(pair) for pair in index["key"]], **options)
            except Exception as e:
                logger.warning(f"[RESTORE] {name}: could not rebuild index {index['name']}: {e}")

        report[name] = {"inserted": inserted, "skipped_duplicates": skipped}
    return report


# --- command line -------------------------------------------------------------------------

def _connect():
    from pymongo import MongoClient

    from app.core.config import settings

    client = MongoClient(settings.mongodb_url, serverSelectionTimeoutMS=10_000)
    return client, client[settings.db_name]


def _root(args) -> Path:
    return Path(args.dir or os.environ.get("BACKUP_DIR", "/backups"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.db_backup")
    parser.add_argument("--dir", help="backup folder (default: $BACKUP_DIR or /backups)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("snapshot")
    sub.add_parser("list")
    sub.add_parser("loop")
    verify = sub.add_parser("verify")
    verify.add_argument("snapshot")
    restore = sub.add_parser("restore")
    restore.add_argument("snapshot")
    restore.add_argument("--target-db", required=True)
    restore.add_argument("--allow-non-empty", action="store_true")
    restore.add_argument("--i-understand-this-writes-to-a-non-test-database", action="store_true",
                         dest="allow_non_disposable")
    args = parser.parse_args(argv)
    root = _root(args)

    if args.command == "list":
        for snap in list_snapshots(root):
            print(snap.name, read_manifest(snap)["total_documents"], "documents")
        return 0

    if args.command == "verify":
        problems = verify_snapshot(Path(args.snapshot))
        print("OK" if not problems else "\n".join(problems))
        return 0 if not problems else 1

    if args.command == "snapshot":
        _client, db = _connect()
        print(json.dumps(run_once(db, root, dt.datetime.now(dt.UTC)), indent=2))
        return 0

    if args.command == "restore":
        client, _db = _connect()
        report = restore_snapshot(Path(args.snapshot), client, args.target_db,
                                  allow_non_disposable=args.allow_non_disposable,
                                  allow_non_empty=args.allow_non_empty)
        print(json.dumps(report, indent=2))
        return 0

    if args.command == "loop":
        hours = float(os.environ.get("BACKUP_INTERVAL_HOURS", "6"))
        logger.info(f"[BACKUP] Snapshots every {hours}h into {root}")
        while True:
            client = None
            try:
                client, db = _connect()
                run_once(db, root, dt.datetime.now(dt.UTC))
            except Exception:
                logger.exception("[BACKUP] Backup cycle failed; will retry at the next interval")
            finally:
                if client is not None:
                    client.close()
            time.sleep(hours * 3600)

    return 2


if __name__ == "__main__":
    sys.exit(main())
