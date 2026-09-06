"""Task 4.4 — sizing the connection pool, and a path to read replicas.

This module is imported once per PROCESS, and task 2.4 gives every live call
its own process. That matters directly here: this client's pool is sized for
however many concurrent Mongo operations ONE call actually makes (a handful
— a transcript write per turn, one tool call's read), not for the whole
fleet of calls the server is running, because each call's process gets its
own separate pool. The number that actually needs to stay under Mongo's own
connection ceiling is (pool size) x (max simultaneous call PROCESSES) x
(number of long-lived processes: the API server, the pool of warm workers) —
see `expected_max_connections()`, which is what a monitoring dashboard
(task 4.9) should actually watch, not any single pool's size in isolation.

Read replicas (task 4.4's other half): the manual's own warning is
reproduced in `read_database` below, and it is the one thing in this file
that would actually mislead someone if they missed it — writing to the
primary and immediately reading from a replica can and will return the
value from before the write, because replication is not instant. Beanie
gives no per-query read-preference override, so this is deliberately its
own separate `Motor`/database handle rather than a flag on the existing one:
using it is an explicit choice at the call site, not an ambient setting that
could silently affect a read that needed the primary.
"""

import motor.motor_asyncio
from beanie import init_beanie
from pymongo import ReadPreference

from app.core.config import settings


def _pool_size() -> int:
    """Task 4.4 — 'calculate the pool size you need from your target
    concurrency,' as the manual asks, made concrete for this project's
    actual shape.

    Explicit override (mongo_max_pool_size) wins outright — an operator who
    set a number meant it. Otherwise: this process serves at most ONE call
    (task 2.4) or, in the long-lived API process, orchestrates many call
    processes without itself running their Mongo queries — the call workers
    do that, in their own processes with their own pools. So the API
    process's own pool only needs to cover its own request handling
    (auth, bot CRUD, the dashboard), and a call worker's pool only needs to
    cover one call's own bursts (a transcript write racing a tool lookup).
    8 comfortably covers either without wasting connections Atlas's free/low
    tier caps tightly.
    """
    return settings.mongo_max_pool_size or 8


def expected_max_connections() -> int:
    """What this deployment could ask MongoDB for, all at once, in the worst
    case. This is the number to compare against Atlas's connection limit,
    not any one process's pool size alone — see the module docstring.
    Exposed for the health/metrics endpoints (tasks 4.7/4.9) rather than
    left as something only discoverable by reading this file.

    Three kinds of process hold a pool, and the warm ones are easy to
    forget:

      - every call in progress, up to max_concurrent_calls (task 4.5);
      - every WARM worker idling in the pool, up to call_worker_pool_max
        (tasks 2.4/4.3). These have already run init_db() — that is the
        whole point of pre-warming them — so they are holding real
        connections before a caller has arrived;
      - this API process itself.

    An earlier version took max() of the first two rather than adding them,
    which understated the real figure by the entire warm pool. Understating
    it is the one direction that actually hurts: the number exists so
    somebody can check headroom against a connection limit, and a check
    that reports less than the truth passes right up until the moment
    connections run out.
    """
    pool = _pool_size()
    processes = settings.max_concurrent_calls + settings.call_worker_pool_max + 1
    return pool * processes


client = motor.motor_asyncio.AsyncIOMotorClient(
    settings.mongodb_url,
    serverSelectionTimeoutMS=5000,
    maxPoolSize=_pool_size(),
    # minPoolSize keeps a couple of connections warm rather than paying a
    # fresh handshake on this process's first query — real cost inside a
    # live call, negligible everywhere else.
    minPoolSize=min(2, _pool_size()),
)
database = client[settings.db_name]

# Task 4.4 — the read-replica handle. Same client (Motor pools per replica
# set member internally; this does not open a second connection pool), a
# different default read preference.
#
# `secondaryPreferred`, not `secondary`: if there is no replica configured
# (the common case today — this project's Atlas tier does not run one),
# `secondary` would have nowhere to read from and every query would fail.
# `secondaryPreferred` reads from a replica when one exists and quietly
# falls back to the primary when it doesn't. It is still gated behind
# `mongo_read_from_secondary` (off by default) rather than used
# unconditionally: an operator turning replicas on is the moment to accept
# their lag, not something this file should decide for them.
#
# Bound to `database` (the primary-reading handle) when the setting is off,
# so `read_database` is always safe to import and use — the flag changes
# what it does, never whether calling it is correct.
read_database = (
    client.get_database(settings.db_name, read_preference=ReadPreference.SECONDARY_PREFERRED)
    if settings.mongo_read_from_secondary
    else database
)


async def init_db(document_models: list):
    await init_beanie(database=database, document_models=document_models)
