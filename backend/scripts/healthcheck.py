"""Task 4.7 — the container health check that restarts a frozen app.

Docker runs this every 30 seconds (deploy/Dockerfile's HEALTHCHECK).

Why it does more than report. Found 2026-09-15: plain Docker (no Swarm, no
Kubernetes) never restarts a container for being unhealthy; it only labels
it. Proven on the production server with two throwaway containers under
`restart: unless-stopped`: one alive but failing its health check stayed up
with restarts=0; one that exited was restarted four times in 40 seconds.
The in-process Watchdog (app/core/health.py) cannot cover a frozen app
either, because it runs on the same event loop that froze. So a frozen app
refused every new call until somebody noticed.

What it does. Each run asks /health once:
  - an answer of 200: healthy; the count of unanswered checks resets;
  - any other answer (503: the database is unreachable, say): the app is
    RESPONSIVE. That is the Watchdog's case, which waits for live calls to
    finish before restarting. Reported as unhealthy, never killed here;
  - no answer at all (timeout, refused, dropped): counted. After
    KILL_AFTER_NO_ANSWERS in a row, the app process is force-stopped; the
    container exits and its restart policy brings it back.

Kept to the standard library: it has to start quickly, finish inside
Docker's 12-second timeout, and work when the app itself is wedged.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

OK = "ok"
UNHEALTHY = "unhealthy"
NO_ANSWER = "no_answer"

HEALTH_URL = "http://localhost:8080/health"

# Longer than /health's slowest HONEST answer (app/core/health.py's
# REPORT_MAX_SECONDS, 5s: a 3s database ping during an outage plus two 1s
# readings), with margin, so a working app reporting an outage is heard as a
# 503 rather than mistaken for a frozen one. Found 2026-09-15 by independent
# review: at 3s, a 503 arriving after 3.05s read as "no answer", and a
# database outage got the app killed. Docker's --timeout (12s) sits above
# this with room for Python to start and write state.
PROBE_TIMEOUT_SECONDS = 8.0

# Three unanswered checks, 30 seconds apart: roughly 90 seconds frozen. Two
# (60s) can be a long garbage collection or a slow blocking call finishing
# on its own (a 13-17s event-loop freeze was measured on 2026-09-14);
# restarting over that would drop live calls for nothing.
KILL_AFTER_NO_ANSWERS = 3

# Startup (database, warm call workers) takes a while. Until the app has
# answered once, failures inside this window are a slow boot, not a freeze.
# Bounded, so an app that hangs during boot is still recovered.
BOOT_GRACE_SECONDS = 180

STATE_FILE = Path("/tmp/voiceagent-healthcheck.json")


@dataclass(frozen=True)
class Process:
    pid: int
    # Kernel start time (clock ticks since boot). Identifies ONE process:
    # Docker's restart keeps the container's files, and the new uvicorn
    # often gets the same pid, so a pid alone would carry the old count over.
    start_ticks: int


def probe(url: str = HEALTH_URL, timeout: float = PROBE_TIMEOUT_SECONDS) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return OK if response.status == 200 else UNHEALTHY
    except urllib.error.HTTPError:
        return UNHEALTHY  # it answered, with an error status
    except OSError:
        # URLError (refused, unreachable), TimeoutError, and a connection
        # dropped mid-response are all OSError: no usable answer.
        return NO_ANSWER


def _read_start_ticks(stat_text: str) -> int:
    # /proc/<pid>/stat is "pid (comm) state ppid ...", and comm may itself
    # contain spaces or parentheses, so fields are counted after the LAST ")".
    fields = stat_text[stat_text.rindex(")") + 2 :].split()
    return int(fields[19])  # field 22 overall: starttime


def find_app_process(proc_root: Path = Path("/proc")) -> Process | None:
    """The uvicorn process serving the app. Not the init process (whose
    argument list also mentions uvicorn) and not a call worker (a
    multiprocessing spawn child)."""
    found: list[Process] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = [a for a in (entry / "cmdline").read_bytes().split(b"\0") if a]
            if not argv:
                continue
            program = Path(argv[0].decode(errors="replace")).name
            if program in ("docker-init", "tini", "init"):
                continue
            args = [a.decode(errors="replace") for a in argv]
            if not any(Path(a).name == "uvicorn" for a in args[:2]) or "main:app" not in args:
                continue
            found.append(Process(pid=int(entry.name), start_ticks=_read_start_ticks((entry / "stat").read_text())))
        except (OSError, ValueError, IndexError):
            continue  # a process that ended mid-scan, or one we may not read
    return min(found, key=lambda p: p.pid) if found else None


def _load(state_file: Path) -> dict:
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state_file: Path, state: dict) -> None:
    try:
        state_file.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass  # a health check must not fail because /tmp is unwritable


def check(
    *,
    state_file: Path,
    probe: Callable[[], str],
    find_app: Callable[[], Process | None],
    kill: Callable[[int], None],
    now: float,
    log: Callable[[str], None],
) -> int:
    """One health check. Returns the exit code Docker reads (0 healthy)."""
    outcome = probe()
    app = find_app()
    identity = [app.pid, app.start_ticks] if app else None

    state = _load(state_file)
    if state.get("process") != identity:
        state = {"process": identity, "first_seen": now, "answered": False, "no_answers": 0}

    if outcome in (OK, UNHEALTHY):
        state["answered"] = True
        state["no_answers"] = 0
        _save(state_file, state)
        return 0 if outcome == OK else 1

    booting = not state.get("answered") and now - state.get("first_seen", now) < BOOT_GRACE_SECONDS
    if not booting:
        state["no_answers"] = state.get("no_answers", 0) + 1
    _save(state_file, state)

    if state["no_answers"] < KILL_AFTER_NO_ANSWERS:
        return 1

    if app is None:
        log("[HEALTHCHECK] The app has not answered, but no uvicorn process was found to restart")
        return 1
    if app.pid == 1:
        log(
            "[HEALTHCHECK] The app has not answered, but it is pid 1 and cannot be force-stopped from "
            "inside the container. Set `init: true` for this service in docker-compose.yml."
        )
        return 1

    log(
        f"[HEALTHCHECK] No answer from {HEALTH_URL} for {state['no_answers']} checks in a row: the app "
        f"is frozen. Force-stopping pid {app.pid} so the container's restart policy restarts it."
    )
    kill(app.pid)
    state["no_answers"] = 0
    _save(state_file, state)
    return 1


def _log_to_container_output(message: str) -> None:
    """Health-check output only lands in `docker inspect`. Writing to the init
    process's stdout puts it in `docker compose logs` too, where people look."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | ERROR    | {message}\n"
    print(line, end="", file=sys.stderr)
    try:
        with open("/proc/1/fd/1", "a") as out:
            out.write(line)
    except OSError:
        pass


def main() -> int:
    return check(
        state_file=STATE_FILE,
        probe=probe,
        find_app=find_app_process,
        kill=lambda pid: os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM)),
        now=time.time(),
        log=_log_to_container_output,
    )


if __name__ == "__main__":
    sys.exit(main())
