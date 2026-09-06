"""Task 4.4 — pool sizing math and the read-replica gate.

Not testing Motor/pymongo itself (a real connection pool is not something to
assert against in a unit test), just the two things this project's own code
decides: how big a pool this process asks for, and whether reads are ever
routed to a replica without an operator having explicitly turned that on.
"""

import importlib

import pytest
from pymongo import ReadPreference

from app.core.config import settings
from app.db import mongo


@pytest.fixture(autouse=True)
def _restore_settings():
    """mongo.py reads settings at IMPORT time (the client is a module-level
    object), so a test that changes settings and reloads the module must put
    settings back afterwards or it leaks into every test that imports
    app.db.mongo next."""
    saved = {
        "mongo_max_pool_size": settings.mongo_max_pool_size,
        "mongo_read_from_secondary": settings.mongo_read_from_secondary,
        "max_concurrent_calls": settings.max_concurrent_calls,
        "call_worker_pool_max": settings.call_worker_pool_max,
    }
    yield
    for key, value in saved.items():
        setattr(settings, key, value)
    importlib.reload(mongo)


def test_an_explicit_pool_size_is_honoured():
    settings.mongo_max_pool_size = 25
    assert mongo._pool_size() == 25


def test_the_default_pool_is_small_because_one_process_serves_at_most_one_call():
    settings.mongo_max_pool_size = 0
    assert mongo._pool_size() == 8


def test_expected_connections_counts_the_warm_pool_too():
    """The warm workers are easy to forget and they matter: they have
    already run init_db() — that is the point of pre-warming them — so they
    hold real connections before any caller arrives.

    An earlier version took max() of the two limits instead of adding
    them, understating the true figure by the entire warm pool. That is the
    one direction that hurts, because this number exists so somebody can
    check headroom against a connection limit, and a check that reports
    less than the truth passes right up until connections run out.
    """
    settings.mongo_max_pool_size = 10
    settings.max_concurrent_calls = 6
    settings.call_worker_pool_max = 4

    # 6 calls + 4 warm workers + 1 API process, each with a pool of 10.
    assert mongo.expected_max_connections() == (6 + 4 + 1) * 10


def test_the_estimate_is_never_lower_than_the_calls_alone_could_need():
    """A guard against the shape of the old bug coming back in any form."""
    settings.mongo_max_pool_size = 8
    settings.max_concurrent_calls = 6
    settings.call_worker_pool_max = 4

    calls_alone = settings.max_concurrent_calls * mongo._pool_size()
    assert mongo.expected_max_connections() > calls_alone


def test_reads_stay_on_the_primary_until_an_operator_turns_replicas_on():
    settings.mongo_read_from_secondary = False
    importlib.reload(mongo)

    assert mongo.read_database is mongo.database, (
        "reads were routed to a replica without mongo_read_from_secondary being set"
    )


def test_the_replica_preference_only_applies_once_enabled():
    settings.mongo_read_from_secondary = True
    importlib.reload(mongo)

    assert mongo.read_database is not mongo.database
    assert mongo.read_database.read_preference == ReadPreference.SECONDARY_PREFERRED
    # The primary handle itself must be untouched — every existing Beanie
    # query in the codebase uses `database`, and none of them opted into
    # replica lag.
    assert mongo.database.read_preference == ReadPreference.PRIMARY
