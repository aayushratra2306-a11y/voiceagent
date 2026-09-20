"""Task 4.8 — load test the system.

============================================================================
DO NOT RUN THIS AGAINST THE PRODUCTION SERVER WITHOUT DELIBERATELY DECIDING
TO. Every simulated call in this script is a REAL call: it opens a real
WebRTC connection, and if it gets past setup it makes the deployed server
run a real Deepgram/Groq/Cartesia pipeline exactly as if a person had
called. That means:

  - It costs real provider minutes/tokens (Deepgram, Groq, Cartesia) —
    against whatever plan/quota those accounts are on. This project's own
    standing rule is "never spend money without being told to" — this
    script is the one piece of Phase 4 that can, simply by being run, and
    that decision belongs to the account holder, not to whoever runs it.
  - Free provider plans run out long before the server does: Groq's free
    plan allows 8,000 tokens a minute for gpt-oss-120b (about two callers
    talking at once with document context), and Cartesia's free plan is
    20,000 characters a month.
  - "Hundreds of simultaneous calls" against a 2-vCPU/4GB VM will exceed
    max_concurrent_calls (task 4.5) and the real memory ceiling well before
    hundreds. That is the number this task exists to measure. Start small.
  - It needs a REAL recorded speech sample, not silence and not a generated
    tone. The manual's own warning: silence skips voice-activity detection
    and speech recognition entirely — most of the actual processor load —
    so a silent test reports a wildly optimistic number that means nothing.
============================================================================

Usage:
    set LOADTEST_PASSWORD=...   (or leave it unset to be asked, hidden)
    python -m scripts.load_test --base-url https://your-domain \
        --accounts loadtest_accounts.json --audio path/to/real_speech.wav \
        --steps 1,2,3,4 --hold-seconds 60 \
        --i-understand-this-costs-real-provider-usage

ONE ACCOUNT PER SIMULATED CALL. The server gives each account exactly one
live call and ends the previous one when the same account starts another
(connect.py, _end_previous_calls_for). --accounts is a JSON list of
{"email": ..., "bot_id": ..., "org_id": ...} (exactly what
loadtest_accounts.py create writes), one entry per simultaneous call at the
largest step, each bot owned by its own account, in its own organisation.
X-Org-Id travels on /connect the same way every other tenant route needs
it. All test accounts share one password, read from LOADTEST_PASSWORD or a
hidden prompt, never from the command line (shell history, process list).

What each simulated caller does: stays quiet for --lead-in-seconds so the
greeting can play, says the recording, pauses for --pause-seconds, and
repeats until --hold-seconds is up. The pause is what lets the server see
the turn end and answer; a recording looped without a break never gets one.

What it measures, per simulated call:
  - connect_latency_s   — sending the WebRTC offer to getting the SDP answer
                           back (POST /connect). Does NOT include this
                           client's own ICE gathering.
  - ice_gathering_s     — that gathering, on its own. It belongs to the
                           machine running this script; if it rises with
                           concurrency, the harness is the bottleneck.
  - ice_connected_s     — until ICE reaches 'connected'.
  - first_audio_s       — until the first AUDIBLE bot frame (the greeting).
                           The server sends silence whenever the bot has
                           nothing to say, so the first frame of any kind
                           means nothing.
  - reply_delays_s      — for each sentence: from this caller's last spoken
                           frame to the bot's first audible frame after a
                           real silence. What a person on the call waits,
                           network included.
  - unanswered          — sentences the bot never answered before the caller
                           spoke again.
  - setup_ok            — whether the call reached 'connected' at all.

The server saves its own per-turn delay (conversation_turns.time_to_speech_ms)
for the same calls; read it afterwards as a cross-check.

Ramps through --steps, holding each level for --hold-seconds, printing the
step's median and 95th percentile reply delay. Stops the ramp as soon as a
step has a call that failed to connect (--keep-going to override): pushing
a server that is already refusing calls to a higher level teaches nothing
and is exactly how a load test turns into an outage. Writes one CSV row per
call to --out.
"""

import argparse
import asyncio
import csv
import fractions
import getpass
import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import aiohttp
import av
import numpy as np
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

SAMPLE_RATE = 48000
FRAME_SECONDS = 0.020
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_SECONDS)

# Root-mean-square level, out of 32767, above which a 20ms frame counts as
# sound. The server's own silence is exact zeros and Opus-decoded silence is
# a few tens at most; speech sits in the thousands.
AUDIBLE_RMS = 300.0

# Access tokens last 15 minutes (config.access_token_expire_minutes). Log in
# again comfortably before that, since a ramp can outlast one token.
TOKEN_REFRESH_AFTER_SECONDS = 12 * 60

# The login route allows 5 per minute from one address.
LOGIN_RETRY_WAIT_SECONDS = 15.0
LOGIN_MAX_ATTEMPTS = 8

# A little over the server's own call-setup limit (connect.py, 45s).
CONNECT_TIMEOUT_SECONDS = 60.0

# A call process killed outright sends no goodbye; aiortc notices only when
# its connectivity checks fail, about 30s later (aioice: 6 misses, 5s apart).
# A shorter hold can end before a killed call is noticed.
MIN_HOLD_SECONDS_TO_SEE_A_KILLED_CALL = 60.0


@dataclass
class CallResult:
    step_concurrency: int
    call_index: int
    setup_ok: bool
    connect_latency_s: float | None = None
    ice_connected_s: float | None = None
    first_audio_s: float | None = None
    ice_gathering_s: float | None = None
    reply_delays_s: list[float] = field(default_factory=list)
    unanswered: int = 0
    spoken_over: int = 0
    # The connection failed or closed during the hold, after setup. How an
    # overloaded server usually fails: the call process is killed, not refused.
    dropped: bool = False
    error: str = ""


@dataclass
class Session:
    email: str
    bot_id: str
    org_id: str
    token: str | None
    obtained_at: float


@dataclass(frozen=True)
class SpeechPlan:
    lead_in_seconds: float = 8.0
    pause_seconds: float = 8.0


# --- accounts and login -------------------------------------------------------


def load_accounts(path: str) -> list[Session]:
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise ValueError("the accounts file must be a non-empty JSON list")
    sessions = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not entry.get("email")
            or not entry.get("bot_id")
            or not entry.get("org_id")
        ):
            raise ValueError('every account needs an "email", a "bot_id" and an "org_id"')
        sessions.append(Session(entry["email"], entry["bot_id"], entry["org_id"], None, 0.0))
    if len({s.email for s in sessions}) != len(sessions):
        raise ValueError("the same email appears twice; each simultaneous call needs its own account")
    return sessions


def needs_login(session: Session, now: float) -> bool:
    return session.token is None or now - session.obtained_at > TOKEN_REFRESH_AFTER_SECONDS


async def login(
    http: aiohttp.ClientSession, base_url: str, email: str, password: str,
    sleep: Callable = asyncio.sleep,
) -> str:
    for attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
        async with http.post(f"{base_url}/auth/login", json={"email": email, "password": password}) as resp:
            if resp.status == 200:
                return (await resp.json())["access_token"]
            if resp.status != 429:
                raise RuntimeError(f"login failed for {email}: HTTP {resp.status}")
            try:
                wait = max(float(resp.headers.get("Retry-After", "")), LOGIN_RETRY_WAIT_SECONDS)
            except ValueError:
                wait = LOGIN_RETRY_WAIT_SECONDS
        print(f"  login for {email} rate limited (attempt {attempt}), waiting {wait:.0f}s")
        await sleep(wait)
    raise RuntimeError(f"login for {email} still rate limited after {LOGIN_MAX_ATTEMPTS} attempts")


async def ensure_logged_in(base_url: str, sessions: list[Session], password: str) -> None:
    async with aiohttp.ClientSession() as http:
        for session in sessions:
            if needs_login(session, time.monotonic()):
                session.token = await login(http, base_url, session.email, password)
                session.obtained_at = time.monotonic()


# --- audio: what the caller says, and what counts as the bot speaking ---------


def is_audible(samples: np.ndarray) -> bool:
    if samples.size == 0:
        return False
    level = math.sqrt(float(np.mean(np.square(samples.astype(np.float64)))))
    return level > AUDIBLE_RMS


def load_speech(path: str) -> np.ndarray:
    """The recording as 48kHz mono 16-bit samples. Refuses one with no speech
    in it: a silent test measures nothing (see the module docstring)."""
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    chunks = []
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            frame.pts = None
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    pcm = np.concatenate(chunks).astype(np.int16) if chunks else np.zeros(0, np.int16)

    usable = len(pcm) - len(pcm) % FRAME_SAMPLES
    frames = pcm[:usable].reshape(-1, FRAME_SAMPLES).astype(np.float64)
    if usable == 0 or not np.any(np.sqrt(np.mean(np.square(frames), axis=1)) > AUDIBLE_RMS):
        raise ValueError(f"{path} has no speech in it; a silent load test measures nothing")
    return pcm


def caller_frames(
    speech: np.ndarray, plan: SpeechPlan, max_frames: int | None = None,
) -> Iterator[tuple[np.ndarray, bool]]:
    """20ms frames: quiet for the lead-in, then the recording, then a pause,
    then the recording again. The flag marks the last frame of each spoken
    sentence, the moment the caller stops talking."""
    silence = np.zeros(FRAME_SAMPLES, np.int16)
    lead_in = int(round(plan.lead_in_seconds / FRAME_SECONDS))
    pause = int(round(plan.pause_seconds / FRAME_SECONDS))
    sentence = [speech[i:i + FRAME_SAMPLES] for i in range(0, len(speech), FRAME_SAMPLES)]
    if sentence and len(sentence[-1]) < FRAME_SAMPLES:
        sentence[-1] = np.concatenate([sentence[-1], silence[: FRAME_SAMPLES - len(sentence[-1])]])
    # The caller stops talking at the last SPOKEN frame, not at the end of the
    # file. Recordings end with quiet; flagging the file's end made every
    # reply look shorter by that much, and lost replies that began inside it.
    spoken = [i for i, chunk in enumerate(sentence) if is_audible(chunk)]
    last_spoken = spoken[-1] if spoken else len(sentence) - 1

    def forever():
        for _ in range(lead_in):
            yield silence, False
        while True:
            for i, chunk in enumerate(sentence):
                yield chunk, i == last_spoken
            for _ in range(pause):
                yield silence, False

    for sent, item in enumerate(forever()):
        if max_frames is not None and sent >= max_frames:
            return
        yield item


class CallerTrack(MediaStreamTrack):
    """The simulated caller's microphone, paced in real time like the real
    thing (same scheme as aiortc's own AudioStreamTrack)."""

    kind = "audio"

    def __init__(self, speech: np.ndarray, plan: SpeechPlan, on_sentence_end: Callable[[float], None]):
        super().__init__()
        self._frames = caller_frames(speech, plan)
        self._on_sentence_end = on_sentence_end
        self._start: float | None = None
        self._timestamp = 0

    async def recv(self) -> av.AudioFrame:
        if self._start is None:
            self._start = time.time()
        else:
            self._timestamp += FRAME_SAMPLES
            wait = self._start + self._timestamp / SAMPLE_RATE - time.time()
            if wait > 0:
                await asyncio.sleep(wait)

        samples, sentence_ended = next(self._frames)
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        if sentence_ended:
            self._on_sentence_end(time.perf_counter())
        return frame


class ReplyTimer:
    """Times each reply from the end of the caller's sentence.

    A reply is the first audible bot frame after the caller finished that
    follows at least min_silence_seconds of quiet. Without the quiet rule, the
    next word after a breath would be recorded as an instant reply to
    something the bot has not even heard yet.

    A sentence the caller finishes while the bot is still talking is not
    timed at all (counted in spoken_over): a gap between two sentences of
    that old answer is indistinguishable, from the audio alone, from a reply.
    "Still talking" means audible at any point in the last
    min_silence_seconds, not just in the last frame: speech dips below the
    loudness line for a few frames between words.
    """

    def __init__(self, min_silence_seconds: float = 0.5):
        self.min_silence_seconds = min_silence_seconds
        self._waiting_since: float | None = None
        self._quiet_since: float | None = None
        self._last_audible_at: float | None = None
        self.start_counting()

    def start_counting(self) -> None:
        """Forget everything measured so far. Called when a step's hold
        begins, so replies given while other calls were still connecting (a
        lower concurrency) are not reported as this step's."""
        self.delays: list[float] = []
        self.unanswered = 0
        self.spoken_over = 0
        self._waiting_since = None

    def caller_finished(self, at: float) -> None:
        if self._waiting_since is not None:
            self.unanswered += 1
        if self._last_audible_at is not None and at - self._last_audible_at < self.min_silence_seconds:
            self.spoken_over += 1
            self._waiting_since = None
        else:
            self._waiting_since = at

    def bot_frame(self, at: float, audible: bool) -> None:
        if audible:
            self._last_audible_at = at
        if not audible:
            if self._quiet_since is None:
                self._quiet_since = at
            return
        if (
            self._waiting_since is not None
            and self._quiet_since is not None
            and at > self._waiting_since
            and at - self._quiet_since >= self.min_silence_seconds
        ):
            self.delays.append(at - self._waiting_since)
            self._waiting_since = None
        self._quiet_since = None


# --- one call, one step, the report ------------------------------------------


async def _listen(track, timer: ReplyTimer, first_audible: dict) -> None:
    """aiortc drops inbound frames nobody recv()s, so the bot's audio has to
    be pulled for the whole call."""
    try:
        while True:
            frame = await track.recv()
            at = time.perf_counter()
            audible = is_audible(frame.to_ndarray())
            if audible:
                first_audible.setdefault("at", at)
            timer.bot_frame(at, audible)
    except MediaStreamError:
        return  # the track ended with the call
    except Exception as e:
        # Swallowing this would look exactly like a bot that never answered.
        print(f"  WARNING: stopped listening to the bot: {type(e).__name__}: {e}", file=sys.stderr)


class StepGate:
    """Every call in a step starts its hold at the same moment: once the last
    one has connected or failed. Otherwise, with cold workers taking 20s+ to
    set up, 'N concurrent calls' overlap for far less than the hold."""

    def __init__(self, calls: int):
        self._left = calls
        self._all_set_up = asyncio.Event()
        if calls <= 0:
            self._all_set_up.set()

    def call_ready(self) -> None:
        self._left -= 1
        if self._left <= 0:
            self._all_set_up.set()

    async def wait(self) -> None:
        await self._all_set_up.wait()


async def run_one_call(
    http: aiohttp.ClientSession, base_url: str, token: str, bot_id: str, org_id: str,
    speech: np.ndarray, plan: SpeechPlan, hold_seconds: float,
    step: int, index: int, ice_servers: list[RTCIceServer], gate: StepGate | None = None,
) -> CallResult:
    result = CallResult(step_concurrency=step, call_index=index, setup_ok=False)
    pc = None
    caller = None
    timer = ReplyTimer()
    listeners: list[asyncio.Task] = []
    first_audible: dict[str, float] = {}
    announced = False

    def announce_set_up() -> None:
        nonlocal announced
        if gate is not None and not announced:
            announced = True
            gate.call_ready()

    try:
        # Built inside the try: an error here must still release the step's
        # gate (finally), or every other call in the step waits forever.
        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))
        caller = CallerTrack(speech, plan, on_sentence_end=timer.caller_finished)
        pc.addTrack(caller)

        @pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                listeners.append(asyncio.ensure_future(_listen(track, timer, first_audible)))

        pc.createDataChannel("pipecat")  # see SessionPage.tsx: must exist before the offer

        # Registered BEFORE setRemoteDescription and re-checked right after:
        # aiortc can reach 'connected' as soon as the answer lands, and the
        # event only fires on a change, so a late listener would report a
        # good call as failed.
        connected = asyncio.Event()
        ended = asyncio.Event()

        @pc.on("iceconnectionstatechange")
        def _on_ice_state():
            if pc.iceConnectionState in ("connected", "completed"):
                connected.set()
            elif pc.iceConnectionState in ("failed", "closed"):
                ended.set()

        @pc.on("connectionstatechange")
        def _on_state():
            if pc.connectionState in ("failed", "closed"):
                ended.set()

        t0 = time.perf_counter()
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)  # aiortc blocks here until ICE gathering completes

        # Timed from here, not t0: gathering is this client's work, not the
        # server's, and grows as this harness itself runs more calls.
        t_request = time.perf_counter()
        result.ice_gathering_s = t_request - t0

        async with http.post(
            f"{base_url}/connect",
            json={"bot_id": bot_id, "sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
            headers={"Authorization": f"Bearer {token}", "X-Org-Id": org_id},
            # The server gives up on a call setup after 45s
            # (connect.py CALL_SETUP_TIMEOUT_SECONDS). Waiting aiohttp's
            # default 300s would keep every other call in the step running,
            # and billing, while this one hangs.
            timeout=aiohttp.ClientTimeout(total=CONNECT_TIMEOUT_SECONDS),
        ) as resp:
            if resp.status != 200:
                # Status first: a proxy's 502/504 page is HTML, and parsing it
                # as JSON would replace the status code with a decode error.
                result.error = f"HTTP {resp.status}: {(await resp.text())[:300]}"
                return result
            body = await resp.json(content_type=None)
            result.connect_latency_s = time.perf_counter() - t_request

        await pc.setRemoteDescription(RTCSessionDescription(sdp=body["sdp"], type=body["type"]))
        if pc.iceConnectionState in ("connected", "completed"):
            connected.set()

        try:
            await asyncio.wait_for(connected.wait(), timeout=20)
            result.ice_connected_s = time.perf_counter() - t_request
            result.setup_ok = True
        except TimeoutError:
            result.error = f"ICE never connected (state={pc.iceConnectionState})"
            return result

        announce_set_up()
        if gate is not None:
            await gate.wait()
        timer.start_counting()
        try:
            await asyncio.wait_for(ended.wait(), timeout=hold_seconds)
            result.dropped = True
            result.error = f"call ended during the hold (connection {pc.connectionState})"
        except TimeoutError:
            pass
        return result
    except Exception as e:
        result.error = f"{type(e).__name__}: {e or 'timed out'}"
        return result
    finally:
        announce_set_up()
        if result.setup_ok:
            if "at" in first_audible:
                result.first_audio_s = first_audible["at"] - t_request
            result.reply_delays_s = list(timer.delays)
            result.unanswered = timer.unanswered
            result.spoken_over = timer.spoken_over
        if caller is not None:
            caller.stop()
        if pc is not None:
            await pc.close()
        for task in listeners:
            task.cancel()


async def run_step(
    base_url: str, sessions: list[Session], speech: np.ndarray, concurrency: int,
    hold_seconds: float, ice_servers: list[RTCIceServer], plan: SpeechPlan,
) -> list[CallResult]:
    """One call per account: sessions[i] places call i."""
    print(f"\n=== Step: {concurrency} concurrent call(s) ===")
    gate = StepGate(concurrency)
    # limit=0: aiohttp's default of 100 connections would queue /connect
    # requests on this side and inflate connect_latency_s past that level.
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as http:
        results = await asyncio.gather(*[
            run_one_call(http, base_url, sessions[i].token, sessions[i].bot_id, sessions[i].org_id, speech, plan,
                         hold_seconds, concurrency, i, ice_servers, gate=gate)
            for i in range(concurrency)
        ])
    return list(results)


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank: always a delay somebody actually waited."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def summarize(results: list[CallResult]) -> dict:
    delays = [d for r in results for d in r.reply_delays_s]
    connects = [r.connect_latency_s for r in results if r.connect_latency_s is not None]
    return {
        "calls": len(results),
        "connected": sum(1 for r in results if r.setup_ok),
        "replies": len(delays),
        "unanswered": sum(r.unanswered for r in results),
        "spoken_over": sum(r.spoken_over for r in results),
        "dropped": sum(1 for r in results if r.dropped),
        "reply_p50_s": _percentile(delays, 50),
        "reply_p95_s": _percentile(delays, 95),
        "connect_p95_s": _percentile(connects, 95),
    }


def should_stop_ramp(summary: dict) -> bool:
    """Any sign the server is already struggling ends the ramp. An overloaded
    server rarely refuses the connection; calls die, or stop being answered
    (a killed call process, a provider quota running out)."""
    return (
        summary["connected"] < summary["calls"]
        or summary["dropped"] > 0
        or summary["replies"] == 0
        or summary["unanswered"] > summary["replies"]
    )


class ResultsWriter:
    """One CSV row per call, flushed after every step, so a failure or Ctrl+C
    at a later step keeps the earlier, already-paid-for results."""

    def __init__(self, path: Path):
        self._file = Path(path).open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=[f.name for f in fields(CallResult)])
        self._writer.writeheader()
        self._file.flush()

    def write(self, results: list[CallResult]) -> None:
        for r in results:
            row = asdict(r)
            row["reply_delays_s"] = ";".join(f"{d:.3f}" for d in r.reply_delays_s)
            self._writer.writerow(row)
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def parse_steps(text: str) -> list[int]:
    try:
        steps = [int(part) for part in text.split(",")]
    except ValueError:
        raise ValueError(f"--steps must be whole numbers separated by commas, got {text!r}") from None
    if not steps or min(steps) < 1:
        raise ValueError(f"--steps must all be at least 1, got {text!r}")
    return steps


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}s"


def _print_step(results: list[CallResult], summary: dict) -> None:
    print(f"  {summary['connected']}/{summary['calls']} connected, {summary['dropped']} dropped, "
          f"{summary['replies']} replies, {summary['unanswered']} unanswered, "
          f"{summary['spoken_over']} spoken over, reply p50 {_fmt(summary['reply_p50_s'])} "
          f"p95 {_fmt(summary['reply_p95_s'])}, connect p95 {_fmt(summary['connect_p95_s'])}")
    for r in results:
        if r.error:
            print(f"    call {r.call_index}: {r.error}")


def _stop_reason(summary: dict) -> str:
    if summary["connected"] < summary["calls"]:
        return f"{summary['calls'] - summary['connected']} call(s) did not connect"
    if summary["dropped"]:
        return f"{summary['dropped']} call(s) ended during the hold"
    if summary["replies"] == 0:
        if summary["spoken_over"]:
            return (f"no reply could be timed: {summary['spoken_over']} sentences were spoken over the "
                    f"bot (its answers outlast --pause-seconds; try a longer pause)")
        return "the bot answered nothing (check --hold-seconds covers lead-in + sentence + pause)"
    return f"{summary['unanswered']} sentences went unanswered against {summary['replies']} answered"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True, help="e.g. https://your-domain (no trailing slash)")
    ap.add_argument("--accounts", required=True,
                    help='JSON list of {"email", "bot_id", "org_id"}, one per simultaneous call')
    ap.add_argument("--audio", required=True, help="A REAL speech recording (wav/mp3), one sentence")
    ap.add_argument("--steps", default="1,2,3", help="Comma-separated concurrency levels to ramp through")
    ap.add_argument("--hold-seconds", type=float, default=60.0, help="How long each step's calls last")
    ap.add_argument("--lead-in-seconds", type=float, default=8.0,
                    help="Quiet at the start of each call, so the greeting can play")
    ap.add_argument("--pause-seconds", type=float, default=8.0,
                    help="Quiet after each sentence, so the bot can answer")
    ap.add_argument("--keep-going", action="store_true",
                    help="Carry on ramping even after a step showed the server struggling")
    ap.add_argument("--out", default="load_test_results.csv")
    ap.add_argument(
        "--i-understand-this-costs-real-provider-usage", action="store_true", dest="confirmed",
        help="Required. See the warning at the top of this file.",
    )
    args = ap.parse_args()

    if not args.confirmed:
        print("Refusing to run: pass --i-understand-this-costs-real-provider-usage once you have "
              "actually read the warning at the top of this file. Every call here is real and "
              "billed exactly like a real caller.", file=sys.stderr)
        return 1

    # Everything that can be checked without the network is checked first.
    try:
        steps = parse_steps(args.steps)
    except ValueError as e:
        print(f"Bad --steps: {e}", file=sys.stderr)
        return 2
    try:
        sessions = load_accounts(args.accounts)
    except (OSError, ValueError) as e:
        print(f"Cannot use the accounts file: {e}", file=sys.stderr)
        return 2
    if max(steps) > len(sessions):
        print(f"The server allows one live call per account, and starting a second call on the "
              f"same account hangs up the first. A step of {max(steps)} calls needs {max(steps)} "
              f"accounts; {args.accounts} has {len(sessions)}.", file=sys.stderr)
        return 2
    try:
        speech = load_speech(args.audio)
    except (OSError, ValueError, av.FFmpegError) as e:
        print(f"Cannot use the recording: {e}", file=sys.stderr)
        return 2

    password = os.environ.get("LOADTEST_PASSWORD") or getpass.getpass("Test account password: ")
    plan = SpeechPlan(lead_in_seconds=args.lead_in_seconds, pause_seconds=args.pause_seconds)

    if args.hold_seconds < MIN_HOLD_SECONDS_TO_SEE_A_KILLED_CALL:
        print(f"WARNING: --hold-seconds {args.hold_seconds:.0f} is short. A call whose process is killed "
              f"is only noticed about 30s later, so a kill in the last 30s of a step goes unreported. "
              f"Use at least {MIN_HOLD_SECONDS_TO_SEE_A_KILLED_CALL:.0f}.", file=sys.stderr)

    try:
        await ensure_logged_in(args.base_url, sessions[:1], password)
    except (RuntimeError, aiohttp.ClientError, TimeoutError) as e:
        print(f"Could not log in: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    async with aiohttp.ClientSession() as http:
        async with http.get(
            f"{args.base_url}/connect/ice-servers", headers={"Authorization": f"Bearer {sessions[0].token}"}
        ) as resp:
            if resp.status != 200:
                print(f"Could not read the ICE servers: HTTP {resp.status}", file=sys.stderr)
                return 2
            servers_json = (await resp.json())["iceServers"]
    ice_servers = [
        RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
        for s in servers_json
    ]

    out_path = Path(args.out)
    writer = ResultsWriter(out_path)
    rows = 0
    try:
        for step in steps:
            try:
                await ensure_logged_in(args.base_url, sessions[:step], password)
            except (RuntimeError, aiohttp.ClientError, TimeoutError) as e:
                print(f"\nStopping before the {step}-call step: {type(e).__name__}: {e}", file=sys.stderr)
                break
            results = await run_step(args.base_url, sessions, speech, step, args.hold_seconds, ice_servers, plan)
            writer.write(results)
            rows += len(results)
            summary = summarize(results)
            _print_step(results, summary)
            if should_stop_ramp(summary) and not args.keep_going:
                print(f"\nStopping the ramp at {step} calls: {_stop_reason(summary)}. Going higher "
                      f"would only push a struggling server further (--keep-going to override).")
                break
    finally:
        writer.close()
        print(f"\nWrote {rows} rows to {out_path}")

    print("Cross-check against the server's own per-turn delay (conversation_turns.time_to_speech_ms) "
          "and its memory and CPU from DURING the run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
