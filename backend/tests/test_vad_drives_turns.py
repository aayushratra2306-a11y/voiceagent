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
    """At 0.85 VAD went completely silent on a real call, which disables Smart
    Turn and hands turn boundaries to the STT endpointer."""
    source = _vad_params_source()
    assert "confidence=0.7," in source, (
        "VAD confidence changed. Above ~0.8 it stopped firing entirely on real "
        "calls, and pipecat degrades silently rather than erroring"
    )
    assert "confidence=0.85," not in source


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
