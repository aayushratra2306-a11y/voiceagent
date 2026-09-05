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
  - "Hundreds of simultaneous calls" against a 2-vCPU/4GB VM (the deployed
    size, per config.py's own notes) will very likely exceed
    max_concurrent_calls (task 4.5) and the real memory ceiling well before
    hundreds — which is not a bug in this script, it is the number this
    task exists to actually measure. Start small.
  - It needs a REAL recorded speech sample, not silence and not a generated
    tone. The manual's own warning: silence skips voice-activity detection
    and speech recognition entirely — most of the actual processor load —
    so a silent test reports a wildly optimistic number that means nothing.
============================================================================

Usage:
    python -m scripts.load_test --base-url https://your-domain \
        --email you@example.com --password ... --bot-id <id> \
        --audio path/to/real_speech.wav \
        --steps 1,2,4,6 --hold-seconds 20

What it measures, per simulated call:
  - connect_latency_s   — time from sending the WebRTC offer to getting the
                           SDP answer back (POST /connect). This is the
                           number task 2.4/4.3's warm-pool work targets.
  - ice_connected_s     — time until the ICE connection actually reaches
                           'connected' — includes STUN/TURN negotiation.
  - first_audio_s       — time until the first inbound audio frame from the
                           bot arrives (the greeting). The closest thing
                           this harness can measure to "time to a human
                           actually hearing something," without doing
                           speech recognition on the result.
  - setup_ok            — whether the call reached 'connected' at all
                           (False rows are exactly what task 4.5's 503 and
                           task 2.4's 504 exist to produce cleanly instead
                           of a hang).

Ramps through --steps (a comma-separated list of concurrency levels),
holding each level for --hold-seconds before closing every call in that
step and moving to the next. Writes one CSV row per call to --out
(default load_test_results.csv) — task 4.8's own instruction is "identify
the actual bottleneck rather than guessing," which means looking at these
per-call rows (and this server's own /metrics and /health while the test
runs — tasks 4.9/4.7), not just an aggregate.
"""

import argparse
import asyncio
import csv
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import aiohttp
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer


@dataclass
class CallResult:
    step_concurrency: int
    call_index: int
    setup_ok: bool
    connect_latency_s: float | None = None
    ice_connected_s: float | None = None
    first_audio_s: float | None = None
    error: str = ""


async def _login(session: aiohttp.ClientSession, base_url: str, email: str, password: str) -> str:
    async with session.post(f"{base_url}/auth/login", json={"email": email, "password": password}) as resp:
        resp.raise_for_status()
        return (await resp.json())["access_token"]


async def _drain_track(track, on_first_frame) -> None:
    """aiortc drops inbound frames that are never recv()'d — a call this
    harness didn't actively drain would silently look like the bot never
    spoke. Keeps pulling until the track ends or the call is torn down."""
    first = True
    try:
        while True:
            await track.recv()
            if first:
                on_first_frame()
                first = False
    except Exception:
        return  # track ended — the call closed, which is expected


async def run_one_call(
    session: aiohttp.ClientSession,
    base_url: str,
    token: str,
    bot_id: str,
    audio_path: str | None,
    hold_seconds: float,
    step: int,
    index: int,
    ice_servers: list[RTCIceServer],
) -> CallResult:
    result = CallResult(step_concurrency=step, call_index=index, setup_ok=False)
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))
    player = None

    try:
        if audio_path:
            player = MediaPlayer(audio_path, loop=True)
            if player.audio:
                pc.addTrack(player.audio)
        if not audio_path or not (player and player.audio):
            print(f"  [call {index}] WARNING: no --audio given — this call sends no audio at "
                  f"all, which the manual explicitly warns skews results optimistic "
                  f"(VAD/STT never engage).", file=sys.stderr)

        first_audio_time: dict[str, float] = {}
        t0 = time.perf_counter()

        @pc.on("track")
        def on_track(track):
            def _mark():
                first_audio_time.setdefault("t", time.perf_counter() - t0)
            asyncio.ensure_future(_drain_track(track, _mark))

        pc.createDataChannel("pipecat")  # see SessionPage.tsx's own note: must exist before the offer

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)  # aiortc blocks here until ICE gathering completes

        headers = {"Authorization": f"Bearer {token}"}
        async with session.post(
            f"{base_url}/connect",
            json={"bot_id": bot_id, "sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
            headers=headers,
        ) as resp:
            body = await resp.json()
            if resp.status != 200:
                result.error = f"HTTP {resp.status}: {body}"
                return result
            result.connect_latency_s = time.perf_counter() - t0

        await pc.setRemoteDescription(RTCSessionDescription(sdp=body["sdp"], type=body["type"]))

        connected = asyncio.Event()

        @pc.on("iceconnectionstatechange")
        def _on_ice_state():
            if pc.iceConnectionState in ("connected", "completed"):
                connected.set()

        try:
            await asyncio.wait_for(connected.wait(), timeout=20)
            result.ice_connected_s = time.perf_counter() - t0
            result.setup_ok = True
        except TimeoutError:
            result.error = f"ICE never connected (state={pc.iceConnectionState})"
            return result

        await asyncio.sleep(hold_seconds)
        result.first_audio_s = first_audio_time.get("t")
        return result
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        return result
    finally:
        if player and player.audio:
            player.audio.stop()
        await pc.close()


async def run_step(
    base_url: str, token: str, bot_id: str, audio_path: str | None,
    concurrency: int, hold_seconds: float, ice_servers: list[RTCIceServer],
) -> list[CallResult]:
    print(f"\n=== Step: {concurrency} concurrent call(s) ===")
    async with aiohttp.ClientSession() as session:
        tasks = [
            run_one_call(session, base_url, token, bot_id, audio_path, hold_seconds,
                         concurrency, i, ice_servers)
            for i in range(concurrency)
        ]
        results = await asyncio.gather(*tasks)

    ok = sum(1 for r in results if r.setup_ok)
    print(f"  {ok}/{concurrency} calls connected")
    for r in results:
        if not r.setup_ok:
            print(f"    call {r.call_index}: FAILED — {r.error}")
    return list(results)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True, help="e.g. https://your-domain (no trailing slash)")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--bot-id", required=True)
    ap.add_argument("--audio", default=None, help="Path to a REAL speech recording (wav/mp3). "
                                                    "See this script's module docstring for why "
                                                    "this matters.")
    ap.add_argument("--steps", default="1,2,4", help="Comma-separated concurrency levels to ramp through")
    ap.add_argument("--hold-seconds", type=float, default=15.0, help="How long each step's calls stay connected")
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

    steps = [int(s) for s in args.steps.split(",")]

    async with aiohttp.ClientSession() as login_session:
        token = await _login(login_session, args.base_url, args.email, args.password)
        async with login_session.get(
            f"{args.base_url}/connect/ice-servers", headers={"Authorization": f"Bearer {token}"}
        ) as resp:
            servers_json = (await resp.json())["iceServers"]
    ice_servers = [
        RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
        for s in servers_json
    ]

    all_results: list[CallResult] = []
    for step in steps:
        results = await run_step(
            args.base_url, token, args.bot_id, args.audio, step, args.hold_seconds, ice_servers,
        )
        all_results.extend(results)

    out_path = Path(args.out)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(all_results[0]).keys()))
        writer.writeheader()
        for r in all_results:
            writer.writerow(asdict(r))

    print(f"\nWrote {len(all_results)} rows to {out_path}")
    print("Cross-check against this server's /health and /metrics from DURING the run, not after — "
          "task 4.8's own instruction is to identify the actual bottleneck, and the breaker/pool/"
          "capacity numbers those endpoints report are what usually shows it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
