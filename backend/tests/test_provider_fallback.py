"""Task 4.6 — the provider half: noticing an outage, and switching away.

Two things are being checked. That the observer turns what the pipeline
already produces into breaker signals without any service having to
cooperate. And that a tripped breaker actually changes which service the
next call is built with — including the case that matters most in practice,
where the backup is not installed and switching to it would make things
worse rather than better.
"""

from pathlib import Path

import pytest

from app.core import breaker
from app.pipeline import provider_health, providers
from app.pipeline.provider_health import (
    LLM_GROQ,
    STT_DEEPGRAM,
    TTS_CARTESIA,
    ProviderHealthObserver,
    fallback_for,
)

# Only the observer tests are async, so the mark goes on those rather than
# the module — a module-level mark makes pytest-asyncio warn on every plain
# function in the file.
asyncio_test = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path, monkeypatch):
    breaker.use_database(tmp_path / "breakers.db")
    monkeypatch.setattr(provider_health.settings, "provider_fallback_enabled", True)
    # backup_is_ready caches for a minute (it is on the call-start and
    # health-check paths); a stale entry would silently decide the next
    # test's answer.
    provider_health._readiness_cache.clear()
    yield
    provider_health._readiness_cache.clear()


def _trip(name: str) -> None:
    for _ in range(provider_health.PROVIDER_BREAKER.failure_threshold):
        breaker.record_failure(name, "connection timed out")


def _ready(monkeypatch, *backups: str) -> None:
    """Pretend the named backups are installed and the others are not."""
    monkeypatch.setattr(
        provider_health,
        "backup_is_ready",
        lambda b: (True, "") if b in backups else (False, "not installed in this test"),
    )


# ---------------------------------------------------------------------------
# Noticing
# ---------------------------------------------------------------------------


class _FakeProcessor:
    """Stands in for a pipecat service. The observer matches on class name,
    so the name is the only thing that has to be right."""

    def __init__(self, name: str):
        self.__class__ = type(name, (_FakeProcessor,), {})


class _Pushed:
    def __init__(self, frame, source=None, processor=None):
        self.frame = frame
        self.source = source
        self.direction = None
        self.timestamp = 0
        self.destination = None
        if processor is not None:
            frame.processor = processor


def _error_frame(text: str, processor):
    from pipecat.frames.frames import ErrorFrame

    return ErrorFrame(error=text, processor=processor)


def _service(class_name: str):
    return type(class_name, (), {})()


@asyncio_test
async def test_a_provider_error_is_reported_to_its_own_breaker():
    observer = ProviderHealthObserver()
    cartesia = _service("ResilientCartesiaTTSService")

    for _ in range(3):
        await observer.on_push_frame(
            _Pushed(_error_frame("websocket handshake failed", cartesia), source=cartesia)
        )

    assert breaker.state(TTS_CARTESIA) == "open"
    assert breaker.state(STT_DEEPGRAM) == "closed", "one provider's failure is not another's"


@asyncio_test
async def test_one_error_travelling_up_the_pipeline_counts_exactly_once():
    """The defect a second read of Phase 4 turned up, and the one that made
    the whole task close to useless.

    pipecat pushes an ErrorFrame UPSTREAM and the observer sees it once per
    hop — about ten of them, since Cartesia sits near the end of the
    pipeline and the frame travels back to the front. `frame.processor`
    keeps naming the ORIGINATING service the whole way, so keying on it
    counted one real failure ten times over: with failure_threshold=3, a
    single transient error opened the breaker instantly and moved every
    subsequent caller onto the local backup.

    Simulated exactly: one frame object, relayed by the real pipeline's
    other processors, each hop still carrying processor=cartesia.
    """
    observer = ProviderHealthObserver()
    cartesia = _service("ResilientCartesiaTTSService")
    frame = _error_frame("websocket handshake failed", cartesia)

    relays = [
        cartesia,                            # hop 1: the provider itself
        _service("TranscriptRecorder"),      # and then back up the pipeline
        _service("MarkdownStripper"),
        _service("GroqLLMService"),
        _service("RAGContextProcessor"),
        _service("LLMUserContextAggregator"),
        _service("RequestBoundary"),
        _service("AudioDebugger"),
        _service("DeepgramSTTService"),
        _service("SmallWebRTCInputTransport"),
    ]
    for hop in relays:
        await observer.on_push_frame(_Pushed(frame, source=hop))

    snapshot = breaker.snapshot()
    assert snapshot[TTS_CARTESIA]["recent_failures"] == 1, (
        f"one error was counted {snapshot[TTS_CARTESIA]['recent_failures']} times — "
        f"every hop up the pipeline recorded it again"
    )
    assert breaker.state(TTS_CARTESIA) == "closed", (
        "a single transient error opened the breaker, so the configured "
        "'3 failures in 45s' threshold was never actually reachable"
    )


@asyncio_test
async def test_an_error_relayed_by_another_provider_is_not_blamed_on_it():
    """The same frame passes back through Deepgram and Groq on its way
    upstream. Neither of them failed."""
    observer = ProviderHealthObserver()
    cartesia = _service("ResilientCartesiaTTSService")
    frame = _error_frame("cartesia is down", cartesia)

    await observer.on_push_frame(_Pushed(frame, source=cartesia))
    await observer.on_push_frame(_Pushed(frame, source=_service("GroqLLMService")))
    await observer.on_push_frame(_Pushed(frame, source=_service("DeepgramSTTService")))

    snapshot = breaker.snapshot()
    assert snapshot[TTS_CARTESIA]["recent_failures"] == 1
    assert LLM_GROQ not in snapshot, "blamed the LLM for the TTS service's error"
    assert STT_DEEPGRAM not in snapshot, "blamed speech recognition for the TTS service's error"


@asyncio_test
async def test_three_separate_errors_still_open_the_breaker():
    """The counting fix must not go so far that real repeated failure stops
    tripping it — three genuinely separate errors are exactly what this is
    configured to act on."""
    observer = ProviderHealthObserver()
    cartesia = _service("ResilientCartesiaTTSService")

    for i in range(3):
        frame = _error_frame(f"handshake failed {i}", cartesia)
        # each one still relayed up the pipeline, as really happens
        await observer.on_push_frame(_Pushed(frame, source=cartesia))
        await observer.on_push_frame(_Pushed(frame, source=_service("MarkdownStripper")))

    assert breaker.state(TTS_CARTESIA) == "open"


@asyncio_test
async def test_a_provider_doing_its_job_closes_its_breaker():
    from pipecat.frames.frames import TTSAudioRawFrame

    observer = ProviderHealthObserver()
    cartesia = _service("ResilientCartesiaTTSService")
    _trip(TTS_CARTESIA)

    # Cooldown has not elapsed, but real audio arriving is proof enough.
    audio = TTSAudioRawFrame(audio=b"\x00\x00", sample_rate=16000, num_channels=1)
    await observer.on_push_frame(_Pushed(audio, source=cartesia))

    assert breaker.state(TTS_CARTESIA) == "closed"


@asyncio_test
async def test_success_is_reported_once_not_per_audio_chunk():
    """Cartesia produces hundreds of audio frames per sentence. Writing to
    the breaker store on every one of them would put a disk write in the
    audio path, which is the last place it belongs."""
    from pipecat.frames.frames import TTSAudioRawFrame

    observer = ProviderHealthObserver()
    cartesia = _service("ResilientCartesiaTTSService")

    writes = []
    original = breaker.record_success
    breaker.record_success = lambda n: writes.append(n) or original(n)
    try:
        for _ in range(50):
            audio = TTSAudioRawFrame(audio=b"\x00\x00", sample_rate=16000, num_channels=1)
            await observer.on_push_frame(_Pushed(audio, source=cartesia))
    finally:
        breaker.record_success = original

    assert len(writes) == 1, f"reported success {len(writes)} times in one call"


@asyncio_test
async def test_a_frame_from_something_unrelated_is_ignored():
    from pipecat.frames.frames import TTSAudioRawFrame

    observer = ProviderHealthObserver()
    audio = TTSAudioRawFrame(audio=b"\x00", sample_rate=16000, num_channels=1)

    await observer.on_push_frame(_Pushed(audio, source=_service("SomeRandomProcessor")))
    await observer.on_push_frame(_Pushed(audio, source=None))

    assert breaker.snapshot() == {}


@asyncio_test
async def test_the_observer_never_raises_into_the_pipeline():
    """A crash in monitoring must not end a call. This is worth a test
    rather than trust: the observer runs on every single frame, so anything
    that can throw in here throws thousands of times a call."""
    observer = ProviderHealthObserver()

    class _Exploding:
        @property
        def frame(self):
            raise RuntimeError("boom")

    await observer.on_push_frame(_Exploding())  # must not raise


# ---------------------------------------------------------------------------
# Switching
# ---------------------------------------------------------------------------


def test_nothing_switches_while_the_provider_is_healthy(monkeypatch):
    _ready(monkeypatch, "whisper", "piper", "openai")
    assert fallback_for(STT_DEEPGRAM) is None
    assert fallback_for(TTS_CARTESIA) is None
    assert fallback_for(LLM_GROQ) is None


def test_a_tripped_provider_switches_to_its_backup(monkeypatch):
    _ready(monkeypatch, "whisper", "piper", "openai")
    _trip(STT_DEEPGRAM)
    _trip(TTS_CARTESIA)
    _trip(LLM_GROQ)

    assert fallback_for(STT_DEEPGRAM) == "whisper"
    assert fallback_for(TTS_CARTESIA) == "piper"
    assert fallback_for(LLM_GROQ) == "openai"


def test_it_refuses_to_switch_to_a_backup_that_is_not_installed(monkeypatch):
    """The most important test in this file.

    Falling back to a Whisper model that is not on disk does not produce a
    slow call — it produces a call that spends its first minute downloading
    500MB while the caller listens to nothing. That is strictly worse than
    the outage, so the switch is skipped and the failing provider is used.
    """
    _ready(monkeypatch)  # nothing installed
    _trip(STT_DEEPGRAM)

    assert fallback_for(STT_DEEPGRAM) is None


def test_an_operator_can_switch_the_whole_thing_off(monkeypatch):
    _ready(monkeypatch, "whisper")
    monkeypatch.setattr(provider_health.settings, "provider_fallback_enabled", False)
    _trip(STT_DEEPGRAM)

    assert fallback_for(STT_DEEPGRAM) is None


def test_the_factory_builds_the_backup_when_the_breaker_is_open(monkeypatch):
    """End to end through the real factory: a tripped Cartesia means the
    next call is constructed with Piper, with no pipeline code involved."""
    _ready(monkeypatch, "piper")
    monkeypatch.setattr(providers.settings, "tts_provider", "cartesia")
    built = {}
    monkeypatch.setattr(
        "pipecat.services.piper.tts.PiperTTSService",
        lambda **kw: built.update(kw) or "piper-service",
    )

    _trip(TTS_CARTESIA)
    service = providers.get_tts_service(voice_id="some-cartesia-voice", language="hi")

    assert service == "piper-service"
    assert built["voice_id"] == providers._PIPER_VOICES["hi"], "fell back to an English voice"


def test_the_factory_still_builds_the_cloud_service_when_all_is_well(monkeypatch):
    _ready(monkeypatch, "piper")
    monkeypatch.setattr(providers.settings, "tts_provider", "cartesia")
    monkeypatch.setattr(providers.settings, "cartesia_api_key", "test-key")

    service = providers.get_tts_service(voice_id="a-voice", language="en")

    assert type(service).__name__ == "ResilientCartesiaTTSService"


def test_an_explicit_local_choice_is_not_undone_by_a_healthy_cloud(monkeypatch):
    """Someone who set tts_provider=piper meant it. Nothing in this task
    should quietly move them back onto a paid provider."""
    _ready(monkeypatch, "piper")
    monkeypatch.setattr(providers.settings, "tts_provider", "piper")
    monkeypatch.setattr(
        "pipecat.services.piper.tts.PiperTTSService", lambda **kw: "piper-service"
    )

    assert providers.get_tts_service(voice_id="v", language="en") == "piper-service"


def test_the_health_report_says_whether_a_backup_would_actually_work():
    """A dashboard that says 'fallback configured' while the model is
    missing is worse than one that says nothing — it is the reason nobody
    checks before the outage."""
    report = provider_health.health()

    assert set(report) == {STT_DEEPGRAM, TTS_CARTESIA, LLM_GROQ}
    for entry in report.values():
        assert entry["state"] == "closed"
        assert "backup_ready" in entry
        if not entry["backup_ready"]:
            assert entry["backup_problem"], "said not ready without saying why"
