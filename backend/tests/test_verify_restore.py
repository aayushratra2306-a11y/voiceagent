"""Task 6.8 — the restore-verification script's own logic.

This is NOT a test that a real Atlas restore works — nothing in this repo
can prove that, and the manual is explicit that only a person actually
performing one can. What IS testable, and what would otherwise ship
unverified, is whether the CHECK itself correctly tells a complete
database from an incomplete one. A verification script that always says
"looks fine" is worse than no script at all — it would turn "I checked"
into a false confidence exactly like the untested backups the manual is
warning about.

Driven against a REAL, throwaway MongoDB database rather than a mock —
the same choice conftest.py already makes for the rest of this suite, and
for the same reason: what matters here is Motor's actual
`list_collection_names()`/`estimated_document_count()` behaviour, not a
guess at what a mock would return for it.
"""

import uuid

import pytest
import pytest_asyncio

from scripts.verify_restore import EXPECTED_COLLECTIONS, GRIDFS_COLLECTIONS, check_database

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _all_model_collection_names() -> set[str]:
    """Reads every Beanie model's own `Settings.name` directly, rather than
    trusting EXPECTED_COLLECTIONS to have kept up — this is the actual
    source of truth, and the whole point of this test is catching drift
    between the two."""
    import importlib
    import inspect
    import pkgutil

    import app.models as models_package

    names = set()
    for module_info in pkgutil.iter_modules(models_package.__path__):
        module = importlib.import_module(f"app.models.{module_info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            settings = getattr(obj, "Settings", None)
            name = getattr(settings, "name", None)
            if name and obj.__module__ == module.__name__:
                names.add(name)
    return names


def test_expected_collections_matches_every_real_model():
    """Found the way this project's other config-drift tests are found:
    by comparing a hand-maintained list against the actual source of
    truth. A new Document model added later and never added here would
    mean a real missing collection after a restore reports as healthy."""
    real = _all_model_collection_names()
    listed = set(EXPECTED_COLLECTIONS)

    missing_from_script = real - listed
    assert not missing_from_script, (
        f"models exist for {missing_from_script} but verify_restore.py never checks "
        f"for them — a restore missing this collection would report as healthy"
    )

    stale_in_script = listed - real
    assert not stale_in_script, (
        f"verify_restore.py checks for {stale_in_script}, which no longer matches "
        f"any real model — likely a renamed collection this list was never updated for"
    )


@pytest_asyncio.fixture(loop_scope="session")
async def throwaway_database():
    """A separate, empty database this test can freely half-populate
    without touching the shared test database every other test in this
    suite relies on."""
    from app.db.mongo import client

    name = f"voiceagent_restore_check_{uuid.uuid4().hex[:8]}"
    db = client[name]
    yield db
    await client.drop_database(name)


async def test_a_database_with_everything_present_is_healthy(throwaway_database):
    """Populated by hand rather than reused from the shared conftest
    database: Beanie/MongoDB does not physically create a collection with
    no index and no document ever written to it, so a freshly-initialised
    ODM connection is NOT the same thing as "every collection exists" —
    only a database that real traffic has actually touched is. A genuine
    production backup would have real traffic behind it; this stands in
    for that explicitly rather than assuming conftest's own fixture
    happens to have exercised every single collection."""
    for name in EXPECTED_COLLECTIONS:
        await throwaway_database[name].insert_one({"probe": True})

    result = await check_database(throwaway_database)

    assert result.healthy, f"missing: {result.missing_collections}"


async def test_a_database_missing_a_collection_is_correctly_flagged_unhealthy(throwaway_database):
    """The scenario this script exists to catch: a restore that silently
    dropped one collection (a wrong region, an incomplete snapshot — the
    manual's own examples) but otherwise looks complete."""
    # Every expected collection created EXCEPT "bots" and "webhook_outbox" —
    # standing in for a restore that lost exactly those two.
    for name in EXPECTED_COLLECTIONS:
        if name in ("bots", "webhook_outbox"):
            continue
        await throwaway_database[name].insert_one({"probe": True})

    result = await check_database(throwaway_database)

    assert not result.healthy
    assert set(result.missing_collections) == {"bots", "webhook_outbox"}


async def test_an_empty_but_present_collection_is_not_treated_as_missing(throwaway_database):
    """The manual's own distinction, encoded directly: an EMPTY collection
    on a fresh deployment (a bot with no bookings yet) is normal. A
    MISSING one is the actual failure signature. Conflating the two would
    make this script cry wolf on every healthy new deployment."""
    for name in EXPECTED_COLLECTIONS:
        await throwaway_database.create_collection(name)
    # Leave "appointments" genuinely empty rather than inserting a probe —
    # this is the exact case being distinguished.

    result = await check_database(throwaway_database)

    assert result.healthy, "an empty (but present) collection was treated as missing"
    assert "appointments" in result.empty_collections
    assert "appointments" not in result.missing_collections


async def test_every_collection_missing_is_reported_not_just_the_first(throwaway_database):
    """A restore verification that stops at the first problem and hides
    the rest would send someone chasing one missing collection, redoing
    the restore, and discovering a second one only on the next attempt."""
    # Nothing created at all — standing in for a restore to entirely the
    # wrong (empty) target database, the "wrong region" case the manual
    # names directly.
    result = await check_database(throwaway_database)

    assert not result.healthy
    assert set(result.missing_collections) == set(EXPECTED_COLLECTIONS)


async def test_gridfs_absence_never_fails_an_otherwise_complete_restore(throwaway_database):
    """GridFS holding no files is a legitimate state for a deployment with
    no knowledge-base uploads yet — task 2.10's own feature is opt-in per
    bot. Every expected Beanie collection is populated here and GridFS is
    deliberately left absent; the restore must still report healthy."""
    for name in EXPECTED_COLLECTIONS:
        await throwaway_database[name].insert_one({"probe": True})

    result = await check_database(throwaway_database)

    assert result.gridfs_present is False
    assert result.healthy, (
        "a restore with every real collection present was marked unhealthy just "
        "because no knowledge-base files had ever been uploaded"
    )


async def test_gridfs_presence_is_detected_when_files_exist(throwaway_database):
    for name in EXPECTED_COLLECTIONS:
        await throwaway_database[name].insert_one({"probe": True})
    await throwaway_database["fs.files"].insert_one({"filename": "test.pdf"})
    await throwaway_database["fs.chunks"].insert_one({"data": b"x"})

    result = await check_database(throwaway_database)

    assert result.gridfs_present is True


def test_gridfs_collections_are_not_double_counted_as_expected_beanie_collections():
    """fs.files/fs.chunks are Motor's own GridFS mechanism, not a Beanie
    Document — they must stay out of EXPECTED_COLLECTIONS or a fresh
    deployment with zero file uploads would permanently report as an
    incomplete restore."""
    assert not (set(GRIDFS_COLLECTIONS) & set(EXPECTED_COLLECTIONS))
