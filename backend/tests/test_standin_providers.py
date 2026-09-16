"""Task 4.8 — the stand-in providers used by the free load test.

These exist so a load test can push many simultaneous calls through the real
server without spending a single provider request. Everything expensive is
replaced; everything that costs CPU on our own machine — WebRTC, VAD, turn
detection, the worker process, the database — is deliberately left alone.

What is being checked here is that each stand-in behaves like the real
service it replaces, in the ways the rest of the pipeline actually depends
on. A stand-in that never produced a transcript, or produced silent audio,
would make a load test report numbers that mean nothing at all.
"""

import time

import numpy as np
import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.tests.utils import run_test

from app.core import health
from app.core.config import settings as app_settings
from app.pipeline import providers
from app.pipeline.standin_providers import (
    StandinLLMService,
    StandinSTTService,
    StandinTTSService,
)
from app.services import rag

SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000

asyncio_test = pytest.mark.asyncio(loop_scope="function")


def _audio(amplitude: int, frames: int) -> list[InputAudioRawFrame]:
    """`frames` 20ms chunks at the given loudness. Amplitude 0 is the exact
    silence a real transport sends between words."""
    samples = np.full(FRAME_SAMPLES, amplitude, dtype=np.int16)
    return [
        InputAudioRawFrame(audio=samples.tobytes(), sample_rate=SAMPLE_RATE, num_channels=1)
        for _ in range(frames)
    ]


def _of_type(frames, frame_type) -> list:
    return [f for f in frames if isinstance(f, frame_type)]


# ---------------------------------------------------------------------------
# Speech recognition
# ---------------------------------------------------------------------------


@asyncio_test
async def test_speech_followed_by_a_pause_produces_one_transcript():
    """The real Deepgram path closes a chunk out as final after 500ms of
    silence (providers.py, endpointing=500). The stand-in has to do the same
    thing, or nothing downstream ever sees a completed turn."""
    stt = StandinSTTService(transcript="hello there", sample_rate=SAMPLE_RATE)

    down, _ = await run_test(
        stt,
        frames_to_send=_audio(6000, 15) + _audio(0, 40),
    )

    transcripts = _of_type(down, TranscriptionFrame)
    assert len(transcripts) == 1
    assert transcripts[0].text == "hello there"


@asyncio_test
async def test_silence_alone_never_produces_a_transcript():
    """A caller who says nothing is transcribed as nothing. Without this the
    load test would measure replies to speech that never happened."""
    stt = StandinSTTService(transcript="hello there", sample_rate=SAMPLE_RATE)

    down, _ = await run_test(stt, frames_to_send=_audio(0, 60))

    assert _of_type(down, TranscriptionFrame) == []


@asyncio_test
async def test_two_separate_utterances_produce_two_transcripts():
    """A load-test caller says its sentence over and over with a pause
    between. Each one has to become its own turn."""
    stt = StandinSTTService(transcript="hello there", sample_rate=SAMPLE_RATE)

    down, _ = await run_test(
        stt,
        frames_to_send=(
            _audio(6000, 15) + _audio(0, 40) + _audio(6000, 15) + _audio(0, 40)
        ),
    )

    assert len(_of_type(down, TranscriptionFrame)) == 2


@asyncio_test
async def test_a_brief_dip_mid_sentence_does_not_split_the_turn():
    """Ordinary speech dips to near-silence between words. Splitting on that
    would report far more turns than a caller actually took."""
    stt = StandinSTTService(transcript="hello there", sample_rate=SAMPLE_RATE)

    down, _ = await run_test(
        stt,
        frames_to_send=(
            _audio(6000, 10) + _audio(0, 5) + _audio(6000, 10) + _audio(0, 40)
        ),
    )

    assert len(_of_type(down, TranscriptionFrame)) == 1


@asyncio_test
async def test_audio_is_passed_downstream():
    """The VAD and turn detector live downstream of speech recognition (see
    the pipeline order in voice_pipeline.py). Swallowing the audio here would
    silently disable both — which is most of the CPU cost a load test exists
    to measure."""
    stt = StandinSTTService(transcript="hello there", sample_rate=SAMPLE_RATE)

    down, _ = await run_test(stt, frames_to_send=_audio(6000, 5))

    assert len(_of_type(down, InputAudioRawFrame)) == 5


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


@asyncio_test
async def test_a_context_frame_produces_a_complete_streamed_reply():
    """Pipecat's aggregators and this project's TranscriptRecorder both key
    off the start/end pair; a reply without them is never saved and never
    closes the turn."""
    llm = StandinLLMService(reply="Yes, I can help with that.", think_seconds=0.0)
    context = LLMContext(messages=[{"role": "user", "content": "hello"}])

    down, _ = await run_test(llm, frames_to_send=[LLMContextFrame(context=context)])

    assert len(_of_type(down, LLMFullResponseStartFrame)) == 1
    assert len(_of_type(down, LLMFullResponseEndFrame)) == 1
    spoken = "".join(f.text for f in _of_type(down, LLMTextFrame))
    assert spoken == "Yes, I can help with that."


@asyncio_test
async def test_the_reply_arrives_in_pieces_rather_than_all_at_once():
    """A real model streams, and this project's TTS starts speaking on the
    first piece. One giant frame would make time-to-first-word look better
    than the real thing ever does."""
    llm = StandinLLMService(reply="Yes, I can help with that.", think_seconds=0.0)
    context = LLMContext(messages=[{"role": "user", "content": "hello"}])

    down, _ = await run_test(llm, frames_to_send=[LLMContextFrame(context=context)])

    assert len(_of_type(down, LLMTextFrame)) > 1


# ---------------------------------------------------------------------------
# The voice
# ---------------------------------------------------------------------------


@asyncio_test
async def test_spoken_text_produces_audible_audio():
    """The load-test client times the gap to the bot's first AUDIBLE frame,
    because the server sends silence whenever the bot has nothing to say. A
    silent stand-in voice would be invisible to every measurement."""
    tts = StandinTTSService(sample_rate=SAMPLE_RATE)

    down, _ = await run_test(tts, frames_to_send=[TTSSpeakFrame(text="Hello there.")])

    audio = _of_type(down, TTSAudioRawFrame)
    assert audio, "the stand-in voice produced no audio at all"
    loudest = max(
        int(np.abs(np.frombuffer(f.audio, dtype=np.int16)).max(initial=0)) for f in audio
    )
    assert loudest > 300, f"audio is effectively silent (peak {loudest})"


@asyncio_test
async def test_the_voice_says_when_it_starts_and_stops_speaking():
    """Without a stop frame the transport only notices the bot went quiet via
    its own 3-second idle fallback, and BotStoppedSpeakingFrame is what closes
    a turn in this pipeline (voice_pipeline.py). A turn closed 3 seconds late
    swallows the caller's next sentence into the previous turn — the merged-
    turn bug this project has already been bitten by once."""
    tts = StandinTTSService(sample_rate=SAMPLE_RATE)

    down, _ = await run_test(tts, frames_to_send=[TTSSpeakFrame(text="Hello there.")])

    assert len(_of_type(down, TTSStartedFrame)) == 1
    assert len(_of_type(down, TTSStoppedFrame)) == 1


@asyncio_test
async def test_the_reply_does_not_begin_until_after_the_thinking_pause():
    """The pause is the whole point of the stand-in model: it stands in for
    real thinking time. Announcing the reply before waiting would record a
    time-to-first-word of nearly zero on every saved turn (TranscriptRecorder
    stamps that moment from this frame), which is the one number the load test
    reads back out of the database afterwards."""
    llm = StandinLLMService(reply="Yes I can.", think_seconds=0.4)
    started_at = None
    announced_at = None
    original_push = llm.push_frame

    async def timed_push(frame, direction=FrameDirection.DOWNSTREAM):
        nonlocal announced_at
        if isinstance(frame, LLMFullResponseStartFrame) and announced_at is None:
            announced_at = time.monotonic()
        await original_push(frame, direction)

    llm.push_frame = timed_push
    context = LLMContext(messages=[{"role": "user", "content": "hello"}])

    started_at = time.monotonic()
    await run_test(llm, frames_to_send=[LLMContextFrame(context=context)])

    assert announced_at is not None, "the reply was never announced"
    assert announced_at - started_at >= 0.4


@asyncio_test
async def test_a_longer_sentence_takes_longer_to_say():
    """Reply audio occupies the call for as long as the bot is speaking. Fixed
    -length audio would let the load test run far more turns per minute than
    a real conversation ever could."""
    short = StandinTTSService(sample_rate=SAMPLE_RATE)
    long = StandinTTSService(sample_rate=SAMPLE_RATE)

    short_down, _ = await run_test(short, frames_to_send=[TTSSpeakFrame(text="Yes.")])
    long_down, _ = await run_test(
        long,
        frames_to_send=[
            TTSSpeakFrame(
                text="Yes, I can certainly help you with that particular question today."
            )
        ],
    )

    def total_samples(frames) -> int:
        return sum(len(f.audio) // 2 for f in _of_type(frames, TTSAudioRawFrame))

    assert total_samples(long_down) > total_samples(short_down) * 2


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


def test_rehearsal_mode_replaces_all_three_paid_services(monkeypatch):
    """One switch, not three. Half a rehearsal — a stand-in ear but the real
    Cartesia voice — still spends money, which is the whole thing this mode
    exists to avoid."""
    monkeypatch.setattr(providers.settings, "standin_providers", True)

    assert isinstance(providers.get_stt_service(language="en"), StandinSTTService)
    assert isinstance(providers.get_llm_service(llm_model="gpt-4o"), StandinLLMService)
    assert isinstance(providers.get_tts_service(voice_id="v", language="en"), StandinTTSService)


def test_a_real_call_is_untouched_when_the_switch_is_off(monkeypatch):
    """The default, and the thing that must never break: with the flag unset
    a caller gets the real providers exactly as before."""
    monkeypatch.setattr(providers.settings, "standin_providers", False)
    monkeypatch.setattr(providers.settings, "tts_provider", "cartesia")
    monkeypatch.setattr(providers.settings, "cartesia_api_key", "test-key")

    service = providers.get_tts_service(voice_id="a-voice", language="en")

    assert type(service).__name__ == "ResilientCartesiaTTSService"


@pytest.mark.asyncio(loop_scope="function")
async def test_rehearsal_mode_makes_no_knowledge_base_requests(monkeypatch):
    """Every call does a knowledge-base lookup, and that path spends OpenAI
    embeddings and Pinecone queries per turn — plus a warm-up per call — even
    for a bot with no documents at all. Left running, a load test would bill
    for search nobody asked for."""
    monkeypatch.setattr(rag.settings, "standin_providers", True)

    async def _explode(*args, **kwargs):
        raise AssertionError("a real search request was made in rehearsal mode")

    monkeypatch.setattr(rag, "_embed", _explode)
    monkeypatch.setattr(rag, "_indexes", _explode)

    assert await rag.query_context("some-bot", "what is on page fifty") == ("", [])


@pytest.mark.asyncio(loop_scope="function")
async def test_rehearsal_mode_makes_no_query_rewrite_requests(monkeypatch):
    """The rewrite runs before every search on Groq's free plan — the exact
    rate limit a load test is trying not to hit."""
    monkeypatch.setattr(rag.settings, "standin_providers", True)

    def _explode():
        raise AssertionError("a real Groq client was built in rehearsal mode")

    monkeypatch.setattr(rag, "_get_groq", _explode)

    assert await rag.rewrite_query("page fifty please") == "page fifty please"


@pytest.mark.asyncio(loop_scope="function")
async def test_the_health_report_says_when_the_server_is_only_rehearsing(monkeypatch):
    """A server stuck in rehearsal mode looks completely healthy while
    answering every caller with a tone. This is the one place an operator can
    see it without reading the logs."""
    monkeypatch.setattr(app_settings, "standin_providers", True)

    assert (await health.report())["standin_providers"] is True
