"""Backups, after the 2026-09-13 incident: production dropped, Atlas had none.

Recovery was only partly possible, from whatever the capped replication log
still held, and anything created before its oldest surviving entry was gone
for good. So the first backup layer lives on the server itself and is taken
automatically, and these tests pin the ways a backup system quietly fails:

  - a half-written snapshot that looks complete,
  - a corrupt or truncated file nobody notices until the restore,
  - retention deleting the last GOOD snapshots after the data was already
    wiped (snapshots of an empty database are still "the newest"),
  - a restore that writes over production when it was meant for a scratch
    database.

No real database is touched anywhere in this file.
"""

import datetime as dt
import gzip
import json
from pathlib import Path

import bson
import pytest
from bson import ObjectId
from pymongo.errors import BulkWriteError

from scripts import db_backup

UTC = dt.UTC
NOW = dt.datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


# --- tiny in-memory stand-ins for a pymongo database ---------------------------------------

class FakeCollection:
    def __init__(self, docs=None, indexes=None, fail_on_read=False):
        self.docs = list(docs or [])
        self.indexes = indexes or {"_id_": {"key": [("_id", 1)], "v": 2}}
        self.fail_on_read = fail_on_read
        self.created_indexes = []

    def find_raw_batches(self):
        if self.fail_on_read:
            raise ConnectionError("connection dropped mid-backup")
        for i in range(0, len(self.docs), 2):
            yield b"".join(bson.encode(d) for d in self.docs[i:i + 2])

    def index_information(self):
        return self.indexes

    def count_documents(self, _filter):
        return len(self.docs)

    def insert_many(self, docs, ordered=False):
        seen = {d["_id"] for d in self.docs}
        errors = []
        for d in docs:
            if d["_id"] in seen:
                errors.append({"code": 11000, "errmsg": "duplicate key"})
            else:
                self.docs.append(d)
                seen.add(d["_id"])
        if errors:
            raise BulkWriteError({"writeErrors": errors, "nInserted": len(docs) - len(errors)})

    def create_index(self, keys, **kwargs):
        self.created_indexes.append((keys, kwargs))


class FakeDatabase:
    def __init__(self, name, collections):
        self.name = name
        self.collections = collections

    def list_collection_names(self):
        return list(self.collections)

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection())


class FakeClient:
    def __init__(self, databases):
        self.databases = databases

    def __getitem__(self, name):
        return self.databases.setdefault(name, FakeDatabase(name, {}))


def _production():
    user_id = ObjectId()
    return FakeDatabase("voiceagent", {
        "users": FakeCollection(
            [{"_id": user_id, "email": "a@b.co", "password_hash": "$bcrypt-sha256$x"}],
            indexes={"_id_": {"key": [("_id", 1)], "v": 2},
                     "email_1": {"key": [("email", 1)], "unique": True, "v": 2}},
        ),
        "bots": FakeCollection([
            {"_id": ObjectId(), "name": "Nitya", "user_id": user_id,
             "created": dt.datetime(2026, 9, 1, tzinfo=UTC).replace(tzinfo=None)},
            {"_id": ObjectId(), "name": "tutor", "user_id": user_id},
            {"_id": ObjectId(), "name": "Good Boy", "user_id": user_id},
        ]),
        "system.views": FakeCollection([{"_id": "ignored"}]),
    })


# --- snapshots ------------------------------------------------------------------------------

def test_a_snapshot_round_trips_every_document_exactly(tmp_path):
    db = _production()
    snap = db_backup.write_snapshot(db, tmp_path, NOW)

    assert snap.name == "20260913T120000Z"
    assert db_backup.verify_snapshot(snap) == []

    restored = db_backup.read_collection(snap, "bots")
    assert restored == db.collections["bots"].docs, "types or values changed on the way through"
    manifest = json.loads((snap / "manifest.json").read_text())
    assert manifest["collections"] == {"bots": 3, "users": 1}
    assert manifest["total_documents"] == 4


def test_system_collections_are_skipped(tmp_path):
    snap = db_backup.write_snapshot(_production(), tmp_path, NOW)
    assert not (snap / "system.views.bson.gz").exists()


def test_indexes_are_kept_so_a_restore_keeps_the_unique_email_rule(tmp_path):
    snap = db_backup.write_snapshot(_production(), tmp_path, NOW)
    meta = json.loads((snap / "users.metadata.json").read_text())
    assert any(ix["name"] == "email_1" and ix.get("unique") for ix in meta["indexes"])


def test_an_interrupted_snapshot_never_looks_complete(tmp_path):
    """A connection dropping halfway must not leave something that retention
    or a restore would treat as a finished backup."""
    db = _production()
    db.collections["users"].fail_on_read = True

    with pytest.raises(ConnectionError):
        db_backup.write_snapshot(db, tmp_path, NOW)

    assert db_backup.list_snapshots(tmp_path) == []
    assert not (tmp_path / "20260913T120000Z").exists()


def test_verify_catches_a_corrupt_file(tmp_path):
    snap = db_backup.write_snapshot(_production(), tmp_path, NOW)
    data = gzip.decompress((snap / "bots.bson.gz").read_bytes())
    (snap / "bots.bson.gz").write_bytes(gzip.compress(data[:-7]))

    problems = db_backup.verify_snapshot(snap)

    assert problems and "bots" in " ".join(problems)


def test_verify_catches_a_missing_collection_file(tmp_path):
    snap = db_backup.write_snapshot(_production(), tmp_path, NOW)
    (snap / "users.bson.gz").unlink()

    assert any("users" in p for p in db_backup.verify_snapshot(snap))


# --- noticing that the data itself disappeared ---------------------------------------------

@pytest.mark.parametrize(("previous", "current", "alarm"), [
    (200, 190, False),   # ordinary churn
    (200, 40, True),     # most of the data vanished
    (200, 0, True),      # the incident
    (3, 0, True),        # small, but everything is gone
    (8, 3, False),       # too small to call a percentage drop meaningful
    (None, 0, False),    # first ever snapshot
])
def test_a_sudden_drop_in_documents_raises_the_alarm(previous, current, alarm):
    assert (db_backup.detect_shrink(previous, current) is not None) is alarm


# --- retention ------------------------------------------------------------------------------

def _names(*ages_hours):
    return [(NOW - dt.timedelta(hours=h)).strftime("%Y%m%dT%H%M%SZ") for h in ages_hours]


def test_everything_from_the_last_seven_days_is_kept():
    names = _names(1, 7, 13, 50, 100, 160)
    assert db_backup.plan_deletions(names, NOW, frozen=False) == []


def test_older_snapshots_thin_to_one_per_day_for_thirty_days():
    # Two snapshots on the same day, ten days ago: the older one goes.
    older, newer = _names(24 * 10 + 6, 24 * 10 + 1)
    deleted = db_backup.plan_deletions(_names(1, 2, 3, 4) + [older, newer], NOW, frozen=False)
    assert deleted == [older]


def test_beyond_thirty_days_snapshots_are_deleted():
    ancient = _names(24 * 45)[0]
    assert db_backup.plan_deletions(_names(1, 2, 3, 4) + [ancient], NOW, frozen=False) == [ancient]


def test_the_newest_few_are_never_deleted_however_old():
    """A server whose backups stopped weeks ago must not delete its last
    copies the day they start again."""
    stale = _names(24 * 60, 24 * 61, 24 * 62, 24 * 63)
    assert db_backup.plan_deletions(stale, NOW, frozen=False) == []


def test_nothing_is_deleted_while_retention_is_frozen():
    ancient = _names(24 * 45, 24 * 46)
    assert db_backup.plan_deletions(_names(1, 2, 3, 4) + ancient, NOW, frozen=True) == []


def test_only_snapshot_folders_are_ever_candidates():
    weird = ["notes", "20260913T120000Z.partial", "RETENTION_FROZEN", "../etc"]
    assert db_backup.plan_deletions(weird + _names(24 * 45), NOW, frozen=False) == []


def test_after_a_wipe_the_good_snapshots_are_frozen_not_rotated_away(tmp_path):
    """The failure that turns a backup system into a second outage: data is
    wiped, snapshots of the empty database keep arriving, and a week later
    retention deletes the last snapshots that still had the data in them."""
    good = _production()
    db_backup.run_once(good, tmp_path, NOW - dt.timedelta(hours=6))

    wiped = FakeDatabase("voiceagent", {})
    report = db_backup.run_once(wiped, tmp_path, NOW)

    assert report["shrink_alarm"]
    assert (tmp_path / "RETENTION_FROZEN").exists()
    assert len(db_backup.list_snapshots(tmp_path)) == 2, "a snapshot was deleted after the wipe"


# --- restore --------------------------------------------------------------------------------

def test_restore_refuses_the_production_database_unless_told_explicitly(tmp_path):
    snap = db_backup.write_snapshot(_production(), tmp_path, NOW)
    client = FakeClient({})

    with pytest.raises(db_backup.RestoreRefused, match="voiceagent"):
        db_backup.restore_snapshot(snap, client, "voiceagent")


def test_restore_refuses_a_target_that_already_has_the_data(tmp_path):
    snap = db_backup.write_snapshot(_production(), tmp_path, NOW)
    client = FakeClient({"voiceagent_restore_test": FakeDatabase(
        "voiceagent_restore_test", {"bots": FakeCollection([{"_id": 1, "name": "already here"}])}
    )})

    with pytest.raises(db_backup.RestoreRefused, match="not empty"):
        db_backup.restore_snapshot(snap, client, "voiceagent_restore_test")


def test_restore_puts_every_document_back_and_rebuilds_indexes(tmp_path):
    source = _production()
    snap = db_backup.write_snapshot(source, tmp_path, NOW)
    client = FakeClient({})

    report = db_backup.restore_snapshot(snap, client, "voiceagent_restore_test")

    target = client["voiceagent_restore_test"]
    assert target.collections["bots"].docs == source.collections["bots"].docs
    assert report["bots"]["inserted"] == 3
    assert any(kw.get("unique") and kw.get("name") == "email_1"
               for _keys, kw in target.collections["users"].created_indexes)


def test_restore_into_a_partly_filled_target_skips_duplicates_instead_of_failing(tmp_path):
    source = _production()
    snap = db_backup.write_snapshot(source, tmp_path, NOW)
    existing = dict(source.collections["bots"].docs[0])
    client = FakeClient({"voiceagent_restore_test": FakeDatabase(
        "voiceagent_restore_test", {"bots": FakeCollection([existing])}
    )})

    report = db_backup.restore_snapshot(snap, client, "voiceagent_restore_test", allow_non_empty=True)

    assert report["bots"] == {"inserted": 2, "skipped_duplicates": 1}


# --- deployment -----------------------------------------------------------------------------

def test_the_backup_service_is_deployed_next_to_the_backend():
    compose = (Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.yml").read_text(
        encoding="utf-8")
    block = compose[compose.index("\n  backup:"):]
    block = block[: block.index("\n  caddy:")] if "\n  caddy:" in block else block

    assert "scripts.db_backup" in block and "loop" in block
    assert "/backups" in block
    assert "../../voiceagent-backups" in block, "backups must live outside the git checkout"
    assert "disable: true" in block, "the backend's HTTP healthcheck would mark this unhealthy"
