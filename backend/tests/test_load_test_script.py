"""Task 4.8 — the load test harness.

Nothing here is run against anything real (see the script's own module
docstring: every simulated call is a real, billed call). What is covered is
the safety net, plus three defects found 2026-09-15 while preparing to run it
for the first time, each of which would have produced a confident, wrong
answer:

1. Every simulated call logged in as the SAME user. connect.py ends a user's
   previous live call whenever that user starts a new one
   (_end_previous_calls_for), so "6 concurrent calls" was really one call at a
   time, each new one hanging up the last.
2. "Time to first bot audio" fired on the first frame of any kind. pipecat's
   transport sends silence whenever the bot has nothing to say
   (TransportParams.audio_out_auto_silence defaults to True), so it measured
   the first silent frame, not the greeting.
3. The recording looped without a break. The caller never paused, so the
   server never saw a turn end and the bot never answered. The delay the task
   exists to measure (caller stops talking -> bot starts answering) never
   happened once.
"""

import asyncio
import json
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import pytest
from aiohttp import web

from scripts import load_test

SCRIPT_DIR = Path(__file__).resolve().parents[1]
FRAME = 960  # 20ms at 48kHz


def _run_script(*args, env_extra=None, timeout=30):
    import os

    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "scripts.load_test", *args],
        cwd=SCRIPT_DIR, capture_output=True, text=True, timeout=timeout, env=env,
    )


def _accounts_file(tmp_path, count):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(
        [
            {"email": f"loadtest{i}@example.com", "bot_id": f"bot{i}", "org_id": f"org{i}"}
            for i in range(count)
        ]
    ))
    return path


def _tone(seconds, amplitude=8000, rate=48000):
    t = np.arange(int(seconds * rate)) / rate
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.int16)


# --- the safety net ---------------------------------------------------------


def test_it_refuses_to_run_without_the_confirmation_flag(tmp_path):
    """Every call this script makes is a real, billed call against real
    provider accounts; it must never fire by accident."""
    result = _run_script(
        "--base-url", "http://localhost:9", "--accounts", str(_accounts_file(tmp_path, 1)),
        "--audio", "missing.wav",
    )
    assert result.returncode == 1
    assert "real and billed" in result.stderr


def test_the_confirmation_flag_is_named_unambiguously():
    result = _run_script("--help")
    assert "--i-understand-this-costs-real-provider-usage" in result.stdout


def test_a_password_is_never_taken_on_the_command_line():
    """A password in the command is saved in shell history and visible to
    every process on the machine. It comes from LOADTEST_PASSWORD or a hidden
    prompt instead."""
    result = _run_script("--help")
    assert "--password" not in result.stdout


# --- defect 1: one account per simultaneous call ----------------------------


def test_it_refuses_a_step_with_more_calls_than_accounts_before_any_request(tmp_path):
    """Two calls on one account would hang each other up. That has to stop
    the run up front, not show up as a quietly wrong result."""
    result = _run_script(
        "--base-url", "http://localhost:9", "--accounts", str(_accounts_file(tmp_path, 2)),
        "--audio", "missing.wav", "--steps", "1,3",
        "--i-understand-this-costs-real-provider-usage",
        env_extra={"LOADTEST_PASSWORD": "x"},
    )
    assert result.returncode == 2
    assert "one live call per account" in result.stderr
    assert "3" in result.stderr


def test_every_call_in_a_step_uses_a_different_account(monkeypatch):
    seen = []

    async def fake_call(session, base_url, token, bot_id, *args, **kwargs):
        seen.append((token, bot_id))
        return load_test.CallResult(step_concurrency=3, call_index=len(seen) - 1, setup_ok=True)

    monkeypatch.setattr(load_test, "run_one_call", fake_call)
    sessions = [
        load_test.Session(f"a{i}@x", f"bot{i}", f"org{i}", f"token{i}", time.monotonic()) for i in range(4)
    ]

    asyncio.run(load_test.run_step("http://x", sessions, np.zeros(FRAME, np.int16), 3, 1.0, [],
                                   load_test.SpeechPlan()))

    assert len(seen) == 3
    assert len({token for token, _ in seen}) == 3
    assert len({bot for _, bot in seen}) == 3


def test_logging_in_waits_and_retries_when_the_server_says_too_many(unused_tcp_port):
    """The login route allows 5 a minute. Six test accounts would otherwise
    fail on the sixth and the run would stop halfway through a ramp."""
    calls = {"n": 0}

    async def login(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return web.json_response({"error": "Rate limit exceeded"}, status=429)
        return web.json_response({"access_token": "tok"})

    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    async def scenario():
        import aiohttp

        app = web.Application()
        app.router.add_post("/auth/login", login)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
        await site.start()
        try:
            async with aiohttp.ClientSession() as session:
                return await load_test.login(
                    session, f"http://127.0.0.1:{unused_tcp_port}", "a@x", "pw", sleep=fake_sleep
                )
        finally:
            await runner.cleanup()

    assert asyncio.run(scenario()) == "tok"
    assert calls["n"] == 2
    assert waits and waits[0] >= 10


def test_load_accounts_rejects_an_entry_without_an_org_id(tmp_path):
    """Task 5.1 — every bot now lives in an organisation, and /connect needs
    to say which one (X-Org-Id). An account entry with no org_id would send
    that header empty and hit the tenant check instead of a clear error
    here, before any request is made."""
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps([{"email": "a@x", "bot_id": "bot0"}]))
    with pytest.raises(ValueError, match='every account needs an "email", a "bot_id" and an "org_id"'):
        load_test.load_accounts(str(path))


def test_load_accounts_accepts_an_entry_with_an_org_id(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps([{"email": "a@x", "bot_id": "bot0", "org_id": "org0"}]))
    sessions = load_test.load_accounts(str(path))
    assert sessions == [load_test.Session("a@x", "bot0", "org0", None, 0.0)]


def test_run_one_call_sends_the_bot_and_its_organisation_as_headers(unused_tcp_port):
    """Task 5.1 — bots are now organisation-scoped, so /connect needs
    X-Org-Id the same way every other tenant route does (org_id threaded
    through from the session, the way bot_id already is)."""
    captured: dict = {}

    async def connect(request):
        captured["headers"] = dict(request.headers)
        return web.Response(status=400, text="no thanks")

    async def scenario():
        import aiohttp

        app = web.Application()
        app.router.add_post("/connect", connect)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
        try:
            async with aiohttp.ClientSession() as http:
                return await load_test.run_one_call(
                    http, f"http://127.0.0.1:{unused_tcp_port}", "tok-1", "bot-1", "org-1", _tone(0.1),
                    load_test.SpeechPlan(), 0.1, 1, 0, [],
                )
        finally:
            await runner.cleanup()

    result = asyncio.run(scenario())
    assert not result.setup_ok
    assert captured["headers"]["Authorization"] == "Bearer tok-1"
    assert captured["headers"]["X-Org-Id"] == "org-1"


def test_a_session_older_than_the_token_lifetime_is_logged_in_again():
    """Access tokens last 15 minutes; a full ramp can run longer than that."""
    fresh = load_test.Session("a@x", "b", "org", "t", obtained_at=1000.0)
    assert not load_test.needs_login(fresh, now=1000.0 + 60)
    assert load_test.needs_login(fresh, now=1000.0 + 13 * 60)
    assert load_test.needs_login(load_test.Session("a@x", "b", "org", None, 0.0), now=1.0)


# --- defect 2: silence is not the bot speaking ------------------------------


def test_silence_does_not_count_as_the_bot_speaking():
    assert not load_test.is_audible(np.zeros(FRAME, np.int16))
    rng = np.random.default_rng(0)
    hiss = rng.integers(-60, 60, FRAME).astype(np.int16)
    assert not load_test.is_audible(hiss), "codec noise on a silent line is not speech"
    assert load_test.is_audible(_tone(0.02))


# --- defect 3: speak, then pause, and time the reply ------------------------


def test_the_caller_waits_for_the_greeting_then_speaks_then_pauses():
    speech = _tone(0.05)  # 2.5 frames: the last one is padded
    plan = load_test.SpeechPlan(lead_in_seconds=0.04, pause_seconds=0.06)
    frames = list(load_test.caller_frames(speech, plan, max_frames=2 + 3 + 3 + 3))

    kinds = ["speech" if load_test.is_audible(f) else "quiet" for f, _ in frames]
    assert kinds == ["quiet"] * 2 + ["speech"] * 3 + ["quiet"] * 3 + ["speech"] * 3
    assert all(len(f) == FRAME for f, _ in frames)
    ends = [i for i, (_, last) in enumerate(frames) if last]
    assert ends == [4, 10], "the last speech frame of each sentence marks the turn's end"


def test_the_sentence_ends_at_its_last_spoken_frame_not_the_end_of_the_file():
    """Review finding (2026-09-15): real recordings end with quiet. Marking
    the end of the FILE shortened every reply by that quiet, and a reply that
    started inside it was never counted and showed up as unanswered."""
    speech = np.concatenate([_tone(0.06), np.zeros(int(0.1 * 48000), np.int16)])  # 3 spoken + 5 quiet frames
    plan = load_test.SpeechPlan(lead_in_seconds=0.0, pause_seconds=0.02)
    frames = list(load_test.caller_frames(speech, plan, max_frames=9))
    ends = [i for i, (_, last) in enumerate(frames) if last]
    assert ends == [2]


def test_a_reply_is_timed_from_the_end_of_the_callers_sentence():
    timer = load_test.ReplyTimer(min_silence_seconds=0.2)
    timer.bot_frame(9.0, audible=False)
    timer.caller_finished(10.0)
    for t in (10.1, 10.3, 10.5):
        timer.bot_frame(t, audible=False)
    timer.bot_frame(11.2, audible=True)
    timer.bot_frame(11.3, audible=True)
    assert timer.delays == [pytest.approx(1.2)]


def test_a_caller_finishing_in_a_dip_between_the_bots_words_has_spoken_over_it():
    """Second review (2026-09-15): speech dips below the loudness line for a
    few frames between words. Judging 'is the bot talking' by the last frame
    alone let a sentence finished inside such a dip be timed against the
    old answer's next sentence gap."""
    timer = load_test.ReplyTimer(min_silence_seconds=0.2)
    timer.bot_frame(9.9, audible=True)
    timer.bot_frame(9.94, audible=False)  # 60ms dip between words
    timer.bot_frame(9.96, audible=False)
    timer.caller_finished(10.0)
    timer.bot_frame(10.02, audible=True)
    for t in (10.1, 10.3, 10.5, 10.7):  # a gap between sentences of the old answer
        timer.bot_frame(t, audible=False)
    timer.bot_frame(11.6, audible=True)
    assert timer.delays == []
    assert timer.spoken_over == 1


def test_counting_restarts_when_the_step_hold_begins():
    """Second review (2026-09-15): a call that connected 20s before the
    slowest one had already been answered at a lower concurrency. Those
    replies must not be reported as this step's."""
    timer = load_test.ReplyTimer(min_silence_seconds=0.2)
    timer.bot_frame(9.0, audible=False)
    timer.caller_finished(10.0)
    timer.bot_frame(11.0, audible=True)
    timer.caller_finished(12.0)
    timer.bot_frame(19.95, audible=True)
    timer.caller_finished(20.0)
    assert timer.delays and timer.unanswered and timer.spoken_over
    timer.start_counting()
    assert (timer.delays, timer.unanswered, timer.spoken_over) == ([], 0, 0)
    for t in (20.1, 20.5):
        timer.bot_frame(t, audible=False)
    timer.caller_finished(30.0)
    timer.bot_frame(31.0, audible=True)
    assert timer.delays == [pytest.approx(1.0)]


def test_a_sentence_spoken_over_the_bot_is_not_timed():
    """Review finding (2026-09-15): if the bot is still talking when the
    caller finishes, any sentence gap in that old answer looks exactly like a
    reply. There is no honest way to tell them apart from the audio, so the
    sentence is counted as spoken-over and left out of the delays."""
    timer = load_test.ReplyTimer(min_silence_seconds=0.2)
    timer.bot_frame(9.9, audible=True)
    timer.caller_finished(10.0)
    timer.bot_frame(10.1, audible=True)
    for t in (10.2, 10.4, 10.6, 10.8):  # a gap between sentences of the OLD answer
        timer.bot_frame(t, audible=False)
    timer.bot_frame(11.6, audible=True)
    assert timer.delays == []
    assert timer.spoken_over == 1
    assert timer.unanswered == 0


def test_a_sentence_the_bot_never_answered_is_counted():
    timer = load_test.ReplyTimer(min_silence_seconds=0.2)
    timer.caller_finished(10.0)
    timer.bot_frame(10.5, audible=False)
    timer.caller_finished(20.0)  # spoke again with no answer in between
    timer.bot_frame(21.0, audible=True)
    assert timer.delays == [pytest.approx(1.0)]
    assert timer.unanswered == 1


def test_a_recording_is_turned_into_48khz_mono(tmp_path):
    path = tmp_path / "speech.wav"
    rate, seconds = 16000, 1.0
    samples = (6000 * np.sin(2 * np.pi * 300 * np.arange(int(rate * seconds)) / rate)).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.repeat(samples, 2).tobytes())

    pcm = load_test.load_speech(str(path))

    assert pcm.dtype == np.int16 and pcm.ndim == 1
    assert abs(len(pcm) - 48000) < 2000
    assert load_test.is_audible(pcm[FRAME * 10:FRAME * 11])


def test_a_silent_recording_is_refused(tmp_path):
    """The manual's own warning: silence skips voice detection and speech
    recognition, so a silent test reports a meaningless number."""
    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(np.zeros(48000, np.int16).tobytes())
    with pytest.raises(ValueError, match="no speech"):
        load_test.load_speech(str(path))


def test_the_caller_track_sends_audio_in_real_time_and_reports_sentence_ends():
    ended = []
    plan = load_test.SpeechPlan(lead_in_seconds=0.0, pause_seconds=0.04)
    track = load_test.CallerTrack(_tone(0.04), plan, on_sentence_end=ended.append)

    async def pull(n):
        start = time.monotonic()
        frames = [await track.recv() for _ in range(n)]
        return frames, time.monotonic() - start

    frames, took = asyncio.run(pull(10))
    assert took >= 0.15, "frames must be paced like a real microphone, not sent all at once"
    assert frames[0].sample_rate == 48000 and frames[0].samples == FRAME
    assert len(ended) >= 2


# --- the report ---------------------------------------------------------------


def test_the_step_summary_reports_the_slow_tail_not_just_the_middle():
    """The manual: track the 95th percentile, never the average."""
    results = [
        load_test.CallResult(2, 0, True, reply_delays_s=[1.0, 1.1, 1.2]),
        load_test.CallResult(2, 1, True, reply_delays_s=[1.0, 5.0], unanswered=1),
        load_test.CallResult(2, 2, False, error="HTTP 503"),
    ]
    summary = load_test.summarize(results)
    assert summary["connected"] == 2
    assert summary["replies"] == 5
    assert summary["unanswered"] == 1
    assert summary["reply_p50_s"] == pytest.approx(1.1)
    assert summary["reply_p95_s"] == pytest.approx(5.0)


def _healthy(i, **kw):
    return load_test.CallResult(2, i, True, reply_delays_s=[1.0, 1.2], **kw)


def test_the_ramp_stops_once_a_step_has_calls_that_did_not_connect():
    """This runs against a real server. Pushing one that is already refusing
    calls to a higher level teaches nothing and risks an outage."""
    ok = load_test.summarize([_healthy(0), _healthy(1)])
    partial = load_test.summarize([_healthy(0), load_test.CallResult(2, 1, False, error="HTTP 503")])
    assert not load_test.should_stop_ramp(ok)
    assert load_test.should_stop_ramp(partial)


def test_the_ramp_stops_when_a_connected_call_dies_or_stops_answering():
    """Review finding (2026-09-15): an overloaded server rarely refuses the
    connection. A call process gets killed for memory, or a provider quota
    runs out, and every call still reads 'connected'. The ramp used to climb
    on regardless, billing more calls into a broken server."""
    dropped = load_test.summarize([_healthy(0), _healthy(1, dropped=True)])
    silent = load_test.summarize([load_test.CallResult(2, i, True, unanswered=3) for i in range(2)])
    mostly_unanswered = load_test.summarize([
        load_test.CallResult(2, 0, True, reply_delays_s=[1.0], unanswered=2),
        load_test.CallResult(2, 1, True, unanswered=1),
    ])
    nothing_measured = load_test.summarize([load_test.CallResult(2, i, True) for i in range(2)])
    assert dropped["dropped"] == 1
    for summary in (dropped, silent, mostly_unanswered, nothing_measured):
        assert load_test.should_stop_ramp(summary), summary


def test_bad_steps_are_refused_before_any_request(tmp_path):
    for steps in ("0", "1,,2", "-1", "two"):
        result = _run_script(
            "--base-url", "http://localhost:9", "--accounts", str(_accounts_file(tmp_path, 3)),
            "--audio", "missing.wav", "--steps", steps,
            "--i-understand-this-costs-real-provider-usage",
            env_extra={"LOADTEST_PASSWORD": "x"},
        )
        assert result.returncode == 2, (steps, result.stderr)
        assert "steps" in result.stderr.lower()


def test_results_are_on_disk_after_every_step(tmp_path):
    """Review finding (2026-09-15): the CSV was written only at the very end,
    so a failed login at step 4, or Ctrl+C, threw away steps 1-3, which had
    already been paid for."""
    path = tmp_path / "out.csv"
    writer = load_test.ResultsWriter(path)
    writer.write([load_test.CallResult(1, 0, True, reply_delays_s=[1.25, 2.5])])
    text = path.read_text(encoding="utf-8")  # read while still open, as after a crash
    assert "reply_delays_s" in text and "1.250;2.500" in text
    writer.write([load_test.CallResult(2, 0, False, error="HTTP 503")])
    writer.close()
    assert path.read_text(encoding="utf-8").count("reply_delays_s") == 1
    assert "HTTP 503" in path.read_text(encoding="utf-8")


def test_a_failed_connect_keeps_its_status_code_even_when_the_body_is_not_json(unused_tcp_port):
    """Review finding (2026-09-15): a 502/504 page from a proxy is HTML. The
    body was parsed before the status was checked, so the result said
    'JSONDecodeError' and the status code, the useful part, was lost."""

    async def bad_gateway(request):
        return web.Response(status=502, text="<html>502 Bad Gateway</html>", content_type="text/html")

    async def scenario():
        import aiohttp

        app = web.Application()
        app.router.add_post("/connect", bad_gateway)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
        try:
            async with aiohttp.ClientSession() as http:
                return await load_test.run_one_call(
                    http, f"http://127.0.0.1:{unused_tcp_port}", "tok", "bot", "org", _tone(0.1),
                    load_test.SpeechPlan(), 0.1, 1, 0, [],
                )
        finally:
            await runner.cleanup()

    result = asyncio.run(scenario())
    assert not result.setup_ok
    assert result.error.startswith("HTTP 502"), result.error


def test_a_call_that_fails_while_being_built_still_releases_the_step(monkeypatch):
    """Second review (2026-09-15): the peer connection and caller track were
    built outside the try, so an error there never told the gate, and every
    other call in the step waited forever."""
    def broken(*args, **kwargs):
        raise RuntimeError("could not build the connection")

    monkeypatch.setattr(load_test, "RTCPeerConnection", broken)

    async def scenario():
        gate = load_test.StepGate(2)
        result = await load_test.run_one_call(
            None, "http://x", "tok", "bot", "org", _tone(0.1), load_test.SpeechPlan(), 0.1, 2, 0, [], gate=gate,
        )
        gate.call_ready()  # the other call
        await asyncio.wait_for(gate.wait(), 1)
        return result

    result = asyncio.run(scenario())
    assert not result.setup_ok and "could not build" in result.error


def test_a_step_holds_only_once_every_call_in_it_is_set_up():
    """Review finding (2026-09-15): each call held for hold_seconds from its
    OWN connect. With cold workers (22s seen live) '4 concurrent calls'
    overlapped for far less than the hold. The hold starts for everyone once
    the last call has connected or failed."""
    async def scenario():
        gate = load_test.StepGate(3)
        waiter = asyncio.ensure_future(gate.wait())
        gate.call_ready()
        gate.call_ready()
        await asyncio.sleep(0.01)
        early = waiter.done()
        gate.call_ready()
        await asyncio.wait_for(waiter, 1)
        return early

    assert asyncio.run(scenario()) is False
