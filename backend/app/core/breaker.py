"""Task 4.6 — circuit breakers.

The failure this exists to stop is not a provider going down. It is a
provider going *slow*. A dead endpoint refuses the connection in
milliseconds and the call recovers; a sick one accepts the connection and
then sits there, so every live call spends its whole timeout waiting, at the
same time, and the caller hears silence. The manual's own tip says it
plainly: thirty seconds of silence on a phone call is functionally the same
as hanging up.

A breaker turns the second case into the first. After a few failures it
stops trying for a while — failing instantly instead of slowly — and the
caller gets the fallback, or a straight answer, rather than dead air.

Three states, the standard ones:

    closed     normal. Calls go through, failures are counted.
    open       tripped. Calls are refused immediately, no request is made.
    half_open  the cooldown has elapsed. Exactly ONE call is let through as
               a trial. If it works the breaker closes; if it fails the
               cooldown starts again.

Why this is written here rather than pulled in from pybreaker (which the
manual suggests): three requirements none of the obvious libraries meet at
once. It has to be async. It has to be shared across PROCESSES, because
task 2.4 runs every call in its own interpreter — a breaker held in one
call's memory learns nothing from the other four calls failing against the
same provider, which is exactly when you need it most. And a trip has to be
observable from outside the process that tripped it, so the API's own
health endpoint can report it.

SQLite is the store, deliberately, and not Redis:

  - It is in the standard library, so a breaker never becomes the reason a
    call fails to start. A breaker that needs a network round trip to a
    service that might itself be down has the problem backwards.
  - It handles cross-process locking correctly on both Linux and Windows,
    which hand-rolled lock files do not.
  - Breaker state is genuinely per-node information. "Can this machine
    reach Cartesia right now" is a fact about this machine's network path;
    sharing it across nodes would let one node's bad route silence a
    provider everywhere. Task 4.1's shared state (the call registry) does
    belong in Redis. This does not.

Writes only happen on a state change or a failure, so the common path —
a healthy provider — is one indexed read of a tiny local file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

# Where the breaker database lives. Inside the backend directory rather than
# the OS temp dir so it is obvious where to look, and so a container's
# filesystem keeps it for the life of that container. It is deliberately NOT
# persisted across restarts: a fresh process should start by trusting its
# providers again, not by inheriting yesterday's outage.
STATE_DIR = Path(__file__).resolve().parents[2] / ".state"
DB_PATH = STATE_DIR / "breakers.db"


@dataclass(frozen=True)
class BreakerConfig:
    """How much failure is too much, and for how long.

    The defaults are tuned for a real-time voice pipeline, which is a harsher
    environment than a web request: there is no spinner to show, so the
    threshold is low and the cooldown is short. Tripping early costs one
    fallback; tripping late costs every caller on the system at once.
    """

    # Consecutive-ish failures inside the window before the breaker opens.
    failure_threshold: int = 3
    # Failures older than this stop counting. Without a window, three
    # failures spread over a week would trip a provider that is fine.
    window_seconds: float = 60.0
    # How long to stay open before letting one trial through.
    cooldown_seconds: float = 30.0


_DEFAULT = BreakerConfig()

# Per-breaker overrides, registered by the code that owns each dependency.
_configs: dict[str, BreakerConfig] = {}

_local = threading.local()

# Bumped by use_database(). Every thread's cached connection carries the
# generation it was opened at, so a switch is picked up by all of them and
# not only by whichever thread happened to call it. See _connect().
_generation = 0


def configure(name: str, config: BreakerConfig) -> None:
    """Give one breaker its own thresholds. Anything not registered uses the
    defaults above."""
    _configs[name] = config


# ── When the store itself fails ───────────────────────────────────────────────
#
# Review finding #1 (2026-09-11). Everything below this line talks to SQLite,
# and until this was added, none of it caught anything.
#
# Where that bites is not obvious from this file. `allows()` is reached from
# providers.get_stt_service / get_llm_service / get_tts_service, via
# provider_health.fallback_for, on EVERY call with the default configuration —
# and that runs after the WebRTC SDP answer has already gone back to the
# caller's browser. A storage error (disk full, a permission problem, a WAL
# lock timeout, a corrupted file) raised out of `allows()`, unwound through
# the whole pipeline construction, and was swallowed by the pipeline task's
# own bare `except Exception`. The caller got a browser showing a connected
# call with no STT, no LLM and no TTS behind it: silence, and nothing on
# /health pointing at why, because a failure on this path never manages to
# write a breaker row either.
#
# So: the breaker is ADVISORY, never load-bearing. It may refuse a call it
# believes is doomed, but a breaker that cannot read its own state has to get
# out of the way rather than take the call down with it. Failing OPEN is the
# only safe direction — the pre-Phase-4 behaviour was no breaker at all, and
# degrading to that is strictly better than dead air.
#
# Failing open silently would be its own bug, though: it trades visible
# breakage for an invisible loss of protection. Hence `storage_error()`,
# which the health endpoint reports.

_storage_error: str | None = None
_storage_error_logged = False


def storage_error() -> str | None:
    """The last storage failure, or None if the store is working.

    Cleared by the next operation that succeeds, so this reports the CURRENT
    state rather than "something went wrong once, hours ago".
    """
    return _storage_error


def forget_storage_error() -> None:
    """Reset the recorded failure. For tests and for an operator who has
    fixed the underlying problem and wants a clean reading."""
    global _storage_error, _storage_error_logged
    _storage_error = None
    _storage_error_logged = False


def _note_storage_failure(operation: str, exc: Exception) -> None:
    global _storage_error, _storage_error_logged
    _storage_error = f"{type(exc).__name__}: {exc}"

    # Logged once per distinct outage, not once per call. Whatever breaks the
    # store breaks it for every call on the machine at once, and a line per
    # tool call per process would bury the thing an operator needs to read.
    if not _storage_error_logged:
        _storage_error_logged = True
        logger.error(
            f"[BREAKER] the breaker store is not usable ({operation}: {_storage_error}). "
            f"Calls are being ALLOWED through unchecked until it recovers — the "
            f"protection is off, but no call will be broken by this. Store: {DB_PATH}"
        )

    _discard_connection()


def _discard_connection() -> None:
    """Drop this thread's cached connection.

    Load-bearing, not tidiness. A failure part-way through a transaction can
    leave the connection inside an open BEGIN IMMEDIATE; reusing it would
    fail every later call on this thread, turning one transient error into a
    permanent one for the life of the process.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        return
    _local.conn = None
    try:
        conn.close()
    except Exception:
        pass  # already broken; there is nothing further to do about it


def _succeeded() -> None:
    """Called after any operation that completed, so a transient failure
    does not latch off for the life of the process."""
    if _storage_error is not None:
        logger.info("[BREAKER] the breaker store is readable again — protection is back on")
        forget_storage_error()


def _end_transaction(conn: sqlite3.Connection) -> None:
    """COMMIT, in a `finally`, without masking whatever sent us there.

    A plain `conn.execute("COMMIT")` in a finally block is a trap: if the
    body raised, the COMMIT very often raises too (the connection is exactly
    as broken as it was a moment ago), and Python then discards the original
    exception in favour of this second one. The real cause — the thing an
    operator needs — is lost and replaced with a confusing "cannot commit"
    from a line that is not where anything went wrong.
    """
    try:
        conn.execute("COMMIT")
    except Exception:
        # Leave the original exception, if any, to propagate untouched. The
        # connection is discarded by _note_storage_failure either way, so a
        # transaction left open cannot poison the next call.
        pass


def config_for(name: str) -> BreakerConfig:
    return _configs.get(name, _DEFAULT)


def _connect() -> sqlite3.Connection:
    """One connection per thread, created on first use.

    A sqlite3.Connection is not safe to share across threads, and this is
    called from FastAPI's executor threads as well as the event loop.

    The generation check is what makes use_database() actually take effect
    everywhere: a connection is bound to the path it was opened with, and
    only the calling thread's could be closed directly. Any OTHER thread
    would have carried on writing to the old file — which in tests means
    one test's breaker state quietly leaking into the next, and on a
    developer machine means a test run writing into the real server's
    store.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "generation", -1) == _generation:
        return conn
    if conn is not None:
        conn.close()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # timeout: how long to wait for another PROCESS's write lock rather than
    # raising "database is locked". Writes here are single-row and take well
    # under a millisecond, so two seconds is a very large margin.
    conn = sqlite3.connect(DB_PATH, timeout=2.0, isolation_level=None)
    # WAL matters for exactly the reason this module exists: without it a
    # writer blocks every reader, so one process recording a failure would
    # stall every other process's breaker check. With it, readers never wait
    # for a writer at all.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS breakers ("
        "  name TEXT PRIMARY KEY,"
        "  failures TEXT NOT NULL DEFAULT '[]',"  # JSON array of timestamps
        "  opened_at REAL,"
        "  trial_at REAL,"
        "  last_reason TEXT,"
        "  trips INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    _local.conn = conn
    _local.generation = _generation
    return conn


def _row(conn: sqlite3.Connection, name: str) -> tuple[list[float], float | None, float | None, str | None, int]:
    cur = conn.execute(
        "SELECT failures, opened_at, trial_at, last_reason, trips FROM breakers WHERE name = ?",
        (name,),
    )
    found = cur.fetchone()
    if found is None:
        return [], None, None, None, 0
    failures, opened_at, trial_at, reason, trips = found
    try:
        stamps = [float(t) for t in json.loads(failures)]
    except (ValueError, TypeError):
        stamps = []
    return stamps, opened_at, trial_at, reason, trips


def state(name: str) -> str:
    """'closed', 'open' or 'half_open' — without consuming the half-open
    trial. Use this for reporting (the health endpoint); use allows() to
    actually decide whether to make a request.

    Returns 'unknown' if the store cannot be read: a reporting path must not
    turn an incident into a 500 on the page an operator opened to diagnose
    it. See storage_error() for what actually went wrong.
    """
    try:
        result = _state(name)
    except Exception as e:
        _note_storage_failure("state", e)
        return "unknown"
    _succeeded()
    return result


def _state(name: str) -> str:
    conn = _connect()
    _, opened_at, _, _, _ = _row(conn, name)
    if opened_at is None:
        return "closed"
    if time.time() - opened_at >= config_for(name).cooldown_seconds:
        return "half_open"
    return "open"


def allows(name: str) -> bool:
    """May this call be attempted?

    Returns False the instant the breaker is open — no request, no timeout,
    no waiting. In the half-open state exactly one caller gets True and
    everyone else gets False until that trial reports back, which is the
    whole point of half-open: a provider recovering from an outage should
    be probed by one call, not stampeded by all of them.

    Returns True if the store cannot be read at all. That is the deliberate
    direction to fail in — see the note above storage_error(). This sits on
    the critical path of a live call, and a breaker that cannot answer must
    get out of the way rather than take the call down with it.
    """
    try:
        result = _allows(name)
    except Exception as e:
        _note_storage_failure("allows", e)
        return True  # fail OPEN: allow the call
    _succeeded()
    return result


def _allows(name: str) -> bool:
    conn = _connect()
    cfg = config_for(name)
    now = time.time()

    # The healthy path first, WITHOUT a transaction — a plain read.
    #
    # This matters more than it looks. Every tool call on every live call
    # goes through here, from its own process (task 2.4), and SQLite allows
    # exactly one writer at a time: opening BEGIN IMMEDIATE up front, as
    # this first did, put a global write lock in front of every customer
    # API request the whole server makes. Six simultaneous calls would have
    # been queueing behind each other for a lock taken to answer "is
    # anything wrong?" — with the answer, virtually always, "no". Under WAL
    # a reader never blocks and never waits.
    _, opened_at, _, _, _ = _row(conn, name)
    if opened_at is None:
        return True  # closed, which is the overwhelmingly common case

    if now - opened_at < cfg.cooldown_seconds:
        return False  # open and still cooling down: also decidable by reading

    # Only a breaker whose cooldown has actually elapsed needs the write
    # lock, because only then is there something to claim. BEGIN IMMEDIATE
    # takes it up front: without it two processes could both read "cooldown
    # elapsed" and both decide they are the trial — the stampede half-open
    # exists to prevent. Re-read inside the transaction, since the state
    # can have changed since the read above.
    conn.execute("BEGIN IMMEDIATE")
    try:
        _, opened_at, trial_at, _, _ = _row(conn, name)

        if opened_at is None:
            return True  # someone else's trial succeeded while we waited

        if now - opened_at < cfg.cooldown_seconds:
            return False

        # Half-open. One trial at a time; a trial that never reports back
        # (its process died mid-call) must not wedge the breaker shut
        # forever, so a stale one is reclaimed after another full cooldown.
        if trial_at is not None and now - trial_at < cfg.cooldown_seconds:
            return False

        conn.execute("UPDATE breakers SET trial_at = ? WHERE name = ?", (now, name))
        logger.info(f"[BREAKER] {name}: cooldown elapsed, letting one trial call through")
        return True
    finally:
        _end_transaction(conn)


def record_success(name: str) -> None:
    """Report that a call worked. Closes a half-open breaker and clears the
    failure history.

    Never raises: this is called from inside a live call's frame handling,
    and there is nothing useful a caller could do about a storage failure
    anyway. See the note above storage_error().
    """
    try:
        _record_success(name)
    except Exception as e:
        _note_storage_failure("record_success", e)
        return
    _succeeded()


def _record_success(name: str) -> None:
    conn = _connect()

    # Checked by reading first, for the same reason as allows(): this runs
    # after EVERY successful tool call in every call process, and the
    # answer is almost always "nothing to write". Taking SQLite's single
    # write lock to discover that would put every customer API request on
    # the server behind one lock, purely to confirm all was well.
    stamps, opened_at, _, _, _ = _row(conn, name)
    if opened_at is None and not stamps:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        stamps, opened_at, _, _, trips = _row(conn, name)
        if opened_at is None and not stamps:
            return  # another process got there first
        if opened_at is not None:
            logger.info(f"[BREAKER] {name}: trial call succeeded, closing the breaker")
        conn.execute(
            "INSERT INTO breakers (name, failures, opened_at, trial_at, last_reason, trips) "
            "VALUES (?, '[]', NULL, NULL, NULL, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "  failures = '[]', opened_at = NULL, trial_at = NULL, last_reason = NULL",
            (name, trips),
        )
    finally:
        _end_transaction(conn)


def record_failure(name: str, reason: str = "") -> None:
    """Report that a call failed, and open the breaker if that was one
    failure too many.

    A failure during a half-open trial re-opens immediately, whatever the
    count says. That is the point of the trial: the provider was asked
    whether it had recovered and the answer was no.

    Never raises, for the same reason record_success() does not.
    """
    try:
        _record_failure(name, reason)
    except Exception as e:
        _note_storage_failure("record_failure", e)
        return
    _succeeded()


def _record_failure(name: str, reason: str = "") -> None:
    conn = _connect()
    cfg = config_for(name)
    now = time.time()

    conn.execute("BEGIN IMMEDIATE")
    try:
        stamps, opened_at, trial_at, _, trips = _row(conn, name)
        was_trial = opened_at is not None and trial_at is not None

        stamps = [t for t in stamps if now - t < cfg.window_seconds]
        stamps.append(now)

        # A fresh trip and a failed trial both genuinely restart the
        # cooldown — the provider was just asked, one way or another, and
        # said no. A third case looks the same at a glance and is not:
        # `len(stamps) >= threshold` can also go True while the breaker is
        # ALREADY open and no trial has been granted yet (trial_at is
        # None), when a call that started before the trip finally reports
        # its own failure. allows() never let that call through as a
        # probe — opened_at already refused it — so it proves nothing
        # about recovery, and moving opened_at for it would let a trickle
        # of such stragglers re-arm the cooldown indefinitely during a
        # real, ongoing outage. Found reviewing this branch: the original
        # code took `if opened_at is None: fresh-open else: trial-failed`,
        # which silently folded this third case into "trial failed" and
        # extended the cooldown for it anyway.
        should_open = was_trial or len(stamps) >= cfg.failure_threshold
        # `should_open` via the count, with the breaker ALREADY open and no
        # trial granted (trial_at is None), is a call that started before
        # the trip finally reporting its own failure. allows() never let it
        # through as a probe -- opened_at already refused it -- so it
        # proves nothing about recovery. Moving opened_at for it anyway
        # would let a trickle of such stragglers re-arm the cooldown
        # indefinitely during a real, ongoing outage. Found reviewing this
        # branch: the original code only branched on `opened_at is None`,
        # which silently folded this case into "trial failed" and extended
        # the cooldown for it regardless.
        reopened_without_a_trial = opened_at is not None and not was_trial and should_open

        if opened_at is None and should_open:
            trips += 1
            # Loud on purpose — the manual asks for every trip to be
            # logged loudly, because a tripped breaker means callers are
            # being served by a fallback and somebody needs to know.
            logger.error(
                f"[BREAKER] {name} OPENED after {len(stamps)} failure(s) in "
                f"{cfg.window_seconds:.0f}s — refusing calls for "
                f"{cfg.cooldown_seconds:.0f}s. Last reason: {reason or 'unknown'}"
            )
            conn.execute(
                "INSERT INTO breakers (name, failures, opened_at, trial_at, last_reason, trips) "
                "VALUES (?, ?, ?, NULL, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "  failures = excluded.failures, opened_at = excluded.opened_at,"
                "  trial_at = NULL, last_reason = excluded.last_reason, trips = excluded.trips",
                (name, json.dumps(stamps), now, reason[:500], trips),
            )
        elif was_trial:
            logger.warning(
                f"[BREAKER] {name}: trial call failed ({reason or 'unknown'}), "
                f"staying open another {cfg.cooldown_seconds:.0f}s"
            )
            conn.execute(
                "INSERT INTO breakers (name, failures, opened_at, trial_at, last_reason, trips) "
                "VALUES (?, ?, ?, NULL, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "  failures = excluded.failures, opened_at = excluded.opened_at,"
                "  trial_at = NULL, last_reason = excluded.last_reason, trips = excluded.trips",
                (name, json.dumps(stamps), now, reason[:500], trips),
            )
        elif reopened_without_a_trial:
            logger.warning(
                f"[BREAKER] {name}: a failure landed while already open with "
                f"no trial in progress ({reason or 'unknown'}) — a call that "
                f"started before the trip, not a probe. Cooldown left as it was."
            )
            conn.execute(
                "INSERT INTO breakers (name, failures, opened_at, trial_at, last_reason, trips) "
                "VALUES (?, ?, NULL, NULL, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "  failures = excluded.failures, last_reason = excluded.last_reason",
                (name, json.dumps(stamps), reason[:500], trips),
            )
        else:
            logger.warning(
                f"[BREAKER] {name}: failure {len(stamps)}/{cfg.failure_threshold} "
                f"({reason or 'unknown'})"
            )
            conn.execute(
                "INSERT INTO breakers (name, failures, opened_at, trial_at, last_reason, trips) "
                "VALUES (?, ?, NULL, NULL, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "  failures = excluded.failures, last_reason = excluded.last_reason",
                (name, json.dumps(stamps), reason[:500], trips),
            )
    finally:
        _end_transaction(conn)


def snapshot() -> dict[str, dict]:
    """Every breaker this node knows about, for the health endpoint and the
    metrics export. Reports state without consuming a half-open trial.

    Returns {} if the store cannot be read — pair it with storage_error() to
    tell "no breakers have tripped" apart from "the store is broken", which
    look identical otherwise.
    """
    try:
        result = _snapshot()
    except Exception as e:
        _note_storage_failure("snapshot", e)
        return {}
    _succeeded()
    return result


def _snapshot() -> dict[str, dict]:
    conn = _connect()
    out: dict[str, dict] = {}
    # fetchall before the loop: state() runs its own query on this same
    # connection, and stepping a live cursor while doing that is asking for
    # trouble.
    rows = conn.execute(
        "SELECT name, failures, opened_at, trial_at, last_reason, trips FROM breakers"
    ).fetchall()
    for name, failures, opened_at, _trial_at, reason, trips in rows:
        try:
            stamps = json.loads(failures)
        except (ValueError, TypeError):
            stamps = []
        out[name] = {
            # _state, not state: a failure part-way through this loop should
            # reach snapshot()'s own handler and be reported as one broken
            # store, rather than quietly producing a dict where some rows
            # say "unknown" and the caller cannot tell why.
            "state": _state(name),
            "recent_failures": len(stamps),
            "opened_at": opened_at,
            "last_reason": reason,
            "trips": trips,
        }
    return out


def reset(name: str | None = None) -> None:
    """Clear one breaker, or all of them. For tests and for an operator who
    knows the provider is back and does not want to wait out the cooldown."""
    conn = _connect()
    if name is None:
        conn.execute("DELETE FROM breakers")
    else:
        conn.execute("DELETE FROM breakers WHERE name = ?", (name,))


def use_database(path: Path) -> None:
    """Point this process's breaker store somewhere else.

    Tests use it so a run never touches the real store; nothing in the
    application calls it.
    """
    global DB_PATH, STATE_DIR, _generation
    DB_PATH = path
    STATE_DIR = path.parent
    _generation += 1
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# Set by the container/host when several nodes share one filesystem, which
# nothing does today but a Kubernetes deployment with a shared volume would.
if os.environ.get("BREAKER_DB_PATH"):
    use_database(Path(os.environ["BREAKER_DB_PATH"]))
