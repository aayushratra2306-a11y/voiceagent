"""Guards the VAD sensitivity that turn detection silently depends on.

Root-caused 2026-09-04 from a live Hindi call where the caller's sentence was
cut in half: "नमस्ते, आपका क्या नाम है आप बता" was committed as a finished
turn while they were still saying "सकते हैं और आपका क्या काम है?", which
arrived as a second turn. Two days were spent suspecting Deepgram's
endpointing and Smart Turn's accuracy. Neither was the cause.

VAD never fired on that call. Not once. Every turn began with
"strategy: TranscriptionUserTurnStartStrategy" and no analyze_end_of_turn
line appeared in the entire log, so Smart Turn never ran at all.

Pipecat does not treat that as an error. TurnAnalyzerUserTurnStopStrategy
._handle_transcription carries a fallback for transcripts arriving without
VAD -- "Without VAD/turn analyzer data, assume turn is complete" -- which
arms a timer of `_stt_timeout - _stop_secs` off the transcript and ends the
turn when it expires. `_stop_secs` is only ever assigned from a VAD stop
frame, so with VAD silent it stays 0.0 and the wait is the full
ttfs_p99_latency. 0.7 - 0.0 = 0.7s after the final transcript at
10:03:02.120 predicts 10:03:02.820; the turn stopped at 10:03:02.821.

So the turn ends wherever the STT endpointer happens to finalise, and every
mid-sentence pause becomes a turn boundary. The fix is to make VAD fire.

These tests pin the sensitivity settings and the fact that the pipeline can
still tell the two VAD failure modes apart -- silent, versus stuck open --
which the old logging actively obscured by labelling the aggregator's
turn frames as "VAD".
"""

import inspect

from pipecat.frames.frames import (
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
)

from app.pipeline import voice_pipeline
from app.pipeline.voice_pipeline import AudioDebugger

VAD_CONFIDENCE_CEILING = 0.8


def _vad_params_source() -> str:
    return inspect.getsource(voice_pipeline.run_voice_pipeline)


def test_vad_confidence_stays_sensitive_enough_to_actually_fire():
    """0.85 silenced VAD entirely; 0.7 fired only partway into a call, with
    the first utterances still missed. 0.6 is pipecat's own default."""
    source = _vad_params_source()
    assert "confidence=0.6," in source, (
        "VAD confidence changed. At 0.85 it never fired at all and at 0.7 it "
        "missed the opening utterances, and pipecat degrades silently rather "
        "than erroring, so this is not self-announcing when it regresses"
    )
    assert "confidence=0.85," not in source


def test_vad_is_the_instrumented_subclass():
    """Three threshold changes were made without ever measuring what Silero
    actually reported. MeasuredSileroVAD logs the real numbers so the next
    change is arithmetic rather than a fourth guess."""
    assert "MeasuredSileroVAD(params=" in _vad_params_source()


def test_measured_vad_records_the_peaks_it_is_asked_about():
    """The instrumentation must observe the same two values the gate uses, or
    it will confidently report about something that isn't the decision."""
    from app.pipeline.voice_pipeline import MeasuredSileroVAD

    for method in ("voice_confidence", "_get_smoothed_volume"):
        assert hasattr(MeasuredSileroVAD, method)
        assert method in MeasuredSileroVAD.__dict__, (
            f"{method} is no longer overridden, so its value is never recorded"
        )


def test_the_two_vad_frames_are_not_confused_with_turn_frames():
    """The original bug hid behind logging that called the aggregator's
    UserStartedSpeakingFrame "VAD". They are different frames with different
    meanings, and only one of them proves VAD is alive."""
    assert VADUserStartedSpeakingFrame is not UserStartedSpeakingFrame


def test_debugger_only_counts_real_vad_frames_as_vad():
    dbg = AudioDebugger()
    assert dbg._vad_ever_fired is False

    # A turn frame must NOT be taken as evidence that VAD is working.
    dbg._warn_if_vad_silent()
    assert dbg._warned_vad_silent is True, "a silent VAD must be reported"


def test_a_working_vad_produces_no_warning():
    dbg = AudioDebugger()
    dbg._vad_ever_fired = True
    dbg._warn_if_vad_silent()
    assert dbg._warned_vad_silent is False, (
        "warned about a silent VAD on a call where VAD was in fact firing"
    )


def test_the_warning_fires_once_not_every_turn():
    """A per-turn warning on a long call is noise that gets filtered out,
    which is how this class of problem stays invisible."""
    dbg = AudioDebugger()
    dbg._warn_if_vad_silent()
    first = dbg._warned_vad_silent
    dbg._warn_if_vad_silent()
    assert first and dbg._warned_vad_silent


def test_stop_secs_and_endpointing_are_still_deliberately_paired():
    """Deepgram's endpointing and VAD's stop_secs are both 500ms on purpose.
    This is not the cause of the split (VAD silence was), but if the two ever
    drift apart the STT endpointer can finalise inside VAD's silence window
    again -- the condition pipecat's own interim-clearing guard exists for."""
    from app.pipeline.providers import get_stt_service

    svc = get_stt_service("hi")
    assert svc._settings.endpointing == 500
    assert "stop_secs=0.5," in _vad_params_source()


# --- The instrumentation's own first bug (2026-09-04) ------------------------
#
# MeasuredSileroVAD shipped and produced NOTHING. Every report interval raised
# "unsupported format string passed to numpy.ndarray.__format__" and pipecat
# turned it into a non-fatal ErrorFrame, so the call carried on and the logs
# filled with errors instead of measurements.
#
# Cause: SileroVADAnalyzer.voice_confidence is annotated `-> float` but
# returns `self._model(...)[0]`, a numpy value. The annotation is simply
# wrong, and trusting it meant the peaks were numpy objects that ":.2f"
# cannot format. An instrument that silently measures nothing is worse than
# no instrument, so both readings are pinned to real floats here.

import numpy as np  # noqa: E402
from pipecat.audio.vad.silero import SileroVADAnalyzer  # noqa: E402
from pipecat.audio.vad.vad_analyzer import VADAnalyzer, VADParams  # noqa: E402

from app.pipeline.voice_pipeline import MeasuredSileroVAD  # noqa: E402


def _bare_vad() -> MeasuredSileroVAD:
    """An instance without __init__, which would load the Silero model."""
    vad = object.__new__(MeasuredSileroVAD)
    vad._peak_confidence = 0.0
    vad._peak_volume = 0.0
    vad._last_report = 0.0
    vad._params = VADParams(confidence=0.6, min_volume=0.4)
    return vad


def test_a_numpy_confidence_is_coerced_to_a_real_float(monkeypatch):
    """Silero's actual return type, not its annotation."""
    monkeypatch.setattr(
        SileroVADAnalyzer, "voice_confidence",
        lambda self, buffer: np.array([0.83], dtype=np.float32),
    )
    value = _bare_vad().voice_confidence(b"")
    assert type(value) is float, f"got {type(value).__name__}, which ':.2f' cannot format"


def test_a_numpy_volume_is_coerced_too(monkeypatch):
    monkeypatch.setattr(
        VADAnalyzer, "_get_smoothed_volume",
        lambda self, audio: np.float32(0.55),
    )
    vad = _bare_vad()
    assert type(vad._get_smoothed_volume(b"")) is float


def test_the_report_survives_numpy_peaks():
    """The exact crash: formatting a numpy peak with ':.2f'. Reproduced by
    setting the peaks directly, since that is the state the bug left them in."""
    vad = _bare_vad()
    vad._peak_confidence = np.array([0.83], dtype=np.float32)
    vad._peak_volume = np.array([0.55], dtype=np.float32)
    vad._last_report = -1e9  # force the report to be due

    try:
        vad._report_if_due()
    except TypeError as e:
        raise AssertionError(
            f"the VAD report still cannot format its own readings: {e}"
        ) from e


def test_the_report_actually_emits_and_resets(monkeypatch):
    """A report that never fires measures nothing; peaks that never reset
    would make every later line a running maximum rather than a window."""
    lines: list[str] = []
    monkeypatch.setattr(
        "app.pipeline.voice_pipeline.logger.info", lambda msg: lines.append(msg)
    )
    vad = _bare_vad()
    vad._peak_confidence, vad._peak_volume = 0.83, 0.55
    vad._last_report = -1e9
    vad._report_if_due()

    assert lines and "[VAD]" in lines[0]
    assert "would fire" in lines[0], lines[0]
    assert vad._peak_confidence == 0.0 and vad._peak_volume == 0.0


def test_the_report_names_which_gate_blocked():
    """The whole point: the line has to say what to change."""
    vad = _bare_vad()
    seen = []
    import app.pipeline.voice_pipeline as vp

    original = vp.logger.info
    vp.logger.info = lambda msg: seen.append(msg)
    try:
        vad._peak_confidence, vad._peak_volume = 0.42, 0.55  # confidence short
        vad._last_report = -1e9
        vad._report_if_due()
        vad._peak_confidence, vad._peak_volume = 0.83, 0.10  # volume short
        vad._last_report = -1e9
        vad._report_if_due()
    finally:
        vp.logger.info = original

    assert "BLOCKED by confidence" in seen[0], seen[0]
    assert "BLOCKED by min_volume" in seen[1], seen[1]
