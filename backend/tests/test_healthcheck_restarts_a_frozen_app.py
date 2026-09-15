"""Task 4.7, found 2026-09-15: a fully frozen backend was never restarted.

The in-process Watchdog restarts the app when a dependency is broken (the
database unreachable), but it runs on the app's own event loop: if that
loop is frozen, the watchdog is frozen with it. health.py's docstring said
the container's HEALTHCHECK covers that case, with "Docker restarting on
repeated failure". Proven false on the production server (Docker 29.1.3,
no Swarm), with two throwaway containers under `restart: unless-stopped`:

    A, alive but failing its health check:  unhealthy, restarts=0 after 40s
    B, exiting with an error:                restarted 4 times in 40s

Plain Docker only labels an unhealthy container. So the check-up itself now
acts: after enough consecutive checks with NO ANSWER, it force-stops the
app process, the container exits, and the restart policy brings it back
(case B). A 503 is an answer, not a freeze: that is the Watchdog's case,
with its live-call deferral, and must never trigger a kill.

This needs `init: true` in docker-compose.yml: without it uvicorn is PID 1,
and the kernel ignores SIGKILL sent to a namespace's PID 1 from inside it.
"""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from app.core import health
from scripts import healthcheck as hc

BACKEND = Path(__file__).resolve().parents[1]
COMPOSE = BACKEND.parent / "deploy" / "docker-compose.yml"
DOCKERFILE = BACKEND.parent / "deploy" / "Dockerfile"

APP = hc.Process(pid=7, start_ticks=1000)


def _run(state_file: Path, outcomes: list[str], *, now_start: float = 10_000.0, app=APP, step: float = 30.0):
    """Runs one check per outcome, like Docker every 30s. Returns the pids killed
    and the exit codes."""
    killed: list[int] = []
    codes: list[int] = []
    for i, outcome in enumerate(outcomes):
        code = hc.check(
            state_file=state_file,
            probe=lambda outcome=outcome: outcome,
            find_app=lambda: app,
            kill=killed.append,
            now=now_start + i * step,
            log=lambda message: None,
        )
        codes.append(code)
    return killed, codes


# --- when it kills, and when it must not ------------------------------------------

def test_a_frozen_app_is_killed_on_the_third_unanswered_check(tmp_path):
    state = tmp_path / "state.json"

    killed, codes = _run(state, [hc.OK, hc.NO_ANSWER, hc.NO_ANSWER, hc.NO_ANSWER])

    assert killed == [APP.pid]
    assert codes == [0, 1, 1, 1]


def test_two_unanswered_checks_are_not_enough(tmp_path):
    """A 60-second stall (a slow request, a big garbage collection) recovers on
    its own; restarting over it would drop calls for nothing."""
    killed, _ = _run(tmp_path / "state.json", [hc.OK, hc.NO_ANSWER, hc.NO_ANSWER])
    assert killed == []


def test_an_answer_in_between_starts_the_count_again(tmp_path):
    killed, _ = _run(
        tmp_path / "state.json",
        [hc.OK, hc.NO_ANSWER, hc.NO_ANSWER, hc.OK, hc.NO_ANSWER, hc.NO_ANSWER],
    )
    assert killed == []


def test_an_unhealthy_answer_is_never_treated_as_a_freeze(tmp_path):
    """503 means the app is responsive and reports a broken dependency. The
    in-process Watchdog owns that case and waits for live calls to finish;
    killing here would bypass that deferral."""
    killed, codes = _run(tmp_path / "state.json", [hc.OK] + [hc.UNHEALTHY] * 10)

    assert killed == []
    assert codes == [0] + [1] * 10


def test_a_slow_boot_is_not_a_freeze(tmp_path):
    """Before the app has ever answered, failures inside the boot grace do not
    count: startup (database, warm workers) takes a while."""
    within_grace = int(hc.BOOT_GRACE_SECONDS // 30)
    killed, _ = _run(tmp_path / "state.json", [hc.NO_ANSWER] * within_grace)
    assert killed == []


def test_an_app_that_hangs_during_boot_is_still_recovered(tmp_path):
    """Grace is bounded: an app that never answers at all is eventually counted."""
    checks = int(hc.BOOT_GRACE_SECONDS // 30) + hc.KILL_AFTER_NO_ANSWERS + 1
    killed, _ = _run(tmp_path / "state.json", [hc.NO_ANSWER] * checks)
    assert killed == [APP.pid]


def test_a_restarted_app_does_not_inherit_the_old_count(tmp_path):
    """Docker's restart keeps the container's files, and the new uvicorn often
    gets the same pid. The count belongs to one process: pid AND start time."""
    state = tmp_path / "state.json"
    _run(state, [hc.OK, hc.NO_ANSWER, hc.NO_ANSWER], now_start=10_000)

    restarted = hc.Process(pid=APP.pid, start_ticks=APP.start_ticks + 5000)
    killed, _ = _run(state, [hc.NO_ANSWER], now_start=20_000, app=restarted)

    assert killed == [], "a fresh process was killed for its predecessor's failures"


def test_it_never_signals_pid_1(tmp_path):
    """Without init: true the app IS pid 1; the kernel would ignore the kill and
    the container would stay frozen. Say so instead of pretending."""
    messages: list[str] = []
    killed: list[int] = []
    pid_one = hc.Process(pid=1, start_ticks=10)
    for i, outcome in enumerate([hc.OK] + [hc.NO_ANSWER] * 4):
        hc.check(
            state_file=tmp_path / "state.json",
            probe=lambda outcome=outcome: outcome,
            find_app=lambda: pid_one,
            kill=killed.append,
            now=10_000 + i * 30,
            log=messages.append,
        )
    assert killed == []
    assert any("pid 1" in m for m in messages)


def test_the_kill_is_logged_loudly_before_it_happens(tmp_path):
    messages: list[str] = []
    order: list[str] = []

    for i, outcome in enumerate([hc.OK, hc.NO_ANSWER, hc.NO_ANSWER, hc.NO_ANSWER]):
        hc.check(
            state_file=tmp_path / "state.json",
            probe=lambda outcome=outcome: outcome,
            find_app=lambda: APP,
            kill=lambda pid: order.append("kill"),
            now=10_000 + i * 30,
            log=lambda m: (messages.append(m), order.append("log")),
        )

    assert order[-2:] == ["log", "kill"]
    assert "[HEALTHCHECK]" in messages[-1] and "restart" in messages[-1]


def test_a_corrupt_state_file_starts_fresh_instead_of_crashing(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{not json", encoding="utf-8")

    killed, codes = _run(state, [hc.OK])

    assert killed == [] and codes == [0]
    assert json.loads(state.read_text(encoding="utf-8"))["answered"] is True


# --- finding the app process ------------------------------------------------------

def _fake_proc(root: Path, pid: int, ppid: int, argv: list[str], start_ticks: int) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(start_ticks)]
    (d / "stat").write_text(f"{pid} ({Path(argv[0]).name}) " + " ".join(fields), encoding="utf-8")


def test_it_finds_uvicorn_and_not_the_init_or_a_call_worker(tmp_path):
    proc = tmp_path / "proc"
    _fake_proc(proc, 1, 0, ["/sbin/docker-init", "--", "uvicorn", "main:app"], 5)
    _fake_proc(proc, 7, 1, ["/usr/local/bin/python", "/usr/local/bin/uvicorn", "main:app", "--port", "8080"], 900)
    _fake_proc(proc, 40, 7, ["/usr/local/bin/python", "-c", "from multiprocessing.spawn import spawn_main"], 950)

    assert hc.find_app_process(proc) == hc.Process(pid=7, start_ticks=900)


def test_it_reads_the_start_time_even_when_the_name_has_spaces(tmp_path):
    proc = tmp_path / "proc"
    d = proc / "7"
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"uvicorn\0main:app\0")
    fields = ["S", "1"] + ["0"] * 17 + ["4242"]
    (d / "stat").write_text("7 (odd name) with spaces) " + " ".join(fields), encoding="utf-8")

    assert hc.find_app_process(proc) == hc.Process(pid=7, start_ticks=4242)


# --- the deployment wiring ----------------------------------------------------------

def test_the_backend_runs_under_an_init_process():
    backend = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["backend"]
    assert backend.get("init") is True, "without init: true the app is pid 1 and cannot be force-stopped"
    assert backend.get("restart") in ("unless-stopped", "always", "on-failure"), "nothing would bring it back"


def test_the_image_health_check_runs_this_script_within_its_timeout():
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    healthcheck = next(line for line in lines if "CMD" in line and "healthcheck" in line)
    header = next(line for line in lines if line.startswith("HEALTHCHECK"))

    assert "scripts/healthcheck.py" in healthcheck
    timeout = float(header.split("--timeout=")[1].split("s")[0])
    assert hc.PROBE_TIMEOUT_SECONDS + 2 <= timeout, "Docker would cut the check off before the probe gives up"


# --- independent review: a slow but working app must never look frozen ---------
#
# The first version gave up after 3s. /health's database ping also waits 3s
# before reporting the database unreachable, so during an Atlas outage every
# 503 arrived just after the probe had given up: read as NO_ANSWER, and the
# app was killed after ~90s, calls or not, bypassing the Watchdog's live-call
# deferral. The review reproduced it: a 503 after 3.05s made probe() return
# "no_answer". Separately, the Redis call count had no timeout at all, so a
# hung Redis could make /health never answer.

def _slow_503_server(delay: float):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's naming
            time.sleep(delay)
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"status":"degraded"}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_database_outage_answer_is_read_as_unhealthy_not_frozen():
    """The review's reproduction: /health's slowest honest answer, a 503 after
    the full database ping timeout, must be heard as an answer."""
    server = _slow_503_server(health.DB_PING_TIMEOUT_SECONDS + 0.05)
    try:
        outcome = hc.probe(f"http://127.0.0.1:{server.server_address[1]}/health")
    finally:
        server.shutdown()
    assert outcome == hc.UNHEALTHY


def test_the_probe_outwaits_the_slowest_honest_health_answer():
    assert hc.PROBE_TIMEOUT_SECONDS >= health.REPORT_MAX_SECONDS + 2, (
        "a slow but working /health would be mistaken for a frozen app"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_a_hung_redis_cannot_stop_health_from_answering(monkeypatch):
    async def db_ok(timeout=None):
        return True, ""

    async def hangs():
        await asyncio.sleep(3600)

    monkeypatch.setattr(health, "check_database", db_ok)
    monkeypatch.setattr("app.core.call_capacity.active_call_count", hangs)

    started = time.perf_counter()
    result = await asyncio.wait_for(health.report(), timeout=health.REPORT_MAX_SECONDS + 1)

    # The contract the health-check script's timeout is built on. (Not tighter:
    # the first report in a fresh process also pays one-off imports for the
    # backup-readiness checks.)
    assert time.perf_counter() - started < health.REPORT_MAX_SECONDS
    assert result["capacity"]["active_calls"] is None, "an unreadable count must read as unknown"
    assert result["healthy"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_a_stuck_breaker_read_cannot_stop_health_from_answering(monkeypatch):
    async def db_ok(timeout=None):
        return True, ""

    async def no_calls():
        return 0

    async def hangs():
        await asyncio.sleep(3600)

    monkeypatch.setattr(health, "check_database", db_ok)
    monkeypatch.setattr("app.core.call_capacity.active_call_count", no_calls)
    monkeypatch.setattr("app.core.breaker.snapshot_async", hangs)

    result = await asyncio.wait_for(health.report(), timeout=health.REPORT_MAX_SECONDS + 1)

    assert result["circuit_breakers"] == {}
    assert result["healthy"] is True
