"""Task 4.1 — the rate limiter's storage backend follows redis_url.

Not testing slowapi/limits' Redis behaviour itself (that's their test
suite's job), just this project's own decision: blank redis_url keeps the
pre-4.1 in-memory store exactly as task 2.5 shipped it, and setting it
switches to Redis storage so multiple API replicas would share one count
instead of each keeping its own.
"""

import importlib

import pytest

from app.core import rate_limit
from app.core.config import settings


@pytest.fixture(autouse=True)
def _restore_settings():
    saved = settings.redis_url
    yield
    settings.redis_url = saved
    importlib.reload(rate_limit)


def test_a_blank_redis_url_keeps_the_in_memory_store():
    settings.redis_url = ""
    importlib.reload(rate_limit)

    # limits' in-memory backend reports this class name; asserting on it
    # (rather than just "did construction not raise") is what actually
    # proves the pre-4.1 behaviour is unchanged.
    assert type(rate_limit.limiter._storage).__name__ == "MemoryStorage"


def test_a_configured_redis_url_switches_the_storage_backend():
    settings.redis_url = "redis://localhost:6379/0"
    importlib.reload(rate_limit)

    assert type(rate_limit.limiter._storage).__name__ == "RedisStorage"
