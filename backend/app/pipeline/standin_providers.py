"""Task 4.8 — stand-in speech, model and voice services for the load test.

The point of a load test here is to find out what THIS server does when many
calls arrive at once: how much memory a call costs, how many worker processes
fit, where replies start to lag. None of that needs a real Deepgram, Groq or
Cartesia request, and going through them makes the test cost money and run
into free-plan rate limits long before the server itself is under any strain
(Groq's free plan allows roughly two simultaneous callers).

So these three replace the paid services, and nothing else. The parts that
actually consume this machine's processor — WebRTC, Opus, Silero VAD, Smart
Turn, one OS process per call, the transcript writes — are untouched and run
exactly as they do on a real call. That is the difference between a load test
that measures the server and one that measures a vendor's rate limiter.

Switched on by STANDIN_PROVIDERS=1 and nothing else; see providers.py for
where that is read, and health.py for how it is reported. What this mode
cannot tell you is how the real providers behave under concurrency — that
answer needs real requests and a plan that allows them.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator

import numpy as np
from loguru import logger
from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.time import time_now_iso8601

# Loud enough to count as speech. The same figure the load-test client uses to
# decide whether a frame it received is audible (scripts/load_test.py), so both
# ends of the test agree on what sound is.
SPEECH_RMS = 300.0

# Silence that ends a turn. Matches the real Deepgram path's endpointing=500
# (providers.py), so a turn ends after the same pause it would on a real call.
END_OF_TURN_SILENCE_SECONDS = 0.5

# Roughly conversational speaking pace, used to decide how long a reply's
# audio should last. Measured against this project's own saved replies rather
# than chosen: ~15 characters a second is a normal speaking rate.
CHARS_PER_SECOND = 15.0

_TTS_TONE_HZ = 220.0
_TTS_AMPLITUDE = 6000

# What every stand-in call hears and says. Ordinary sentences of ordinary
# length: the reply's length decides how long the bot occupies the call
# speaking, which is part of what sets how many turns a minute holds.
STANDIN_TRANSCRIPT = "Hello, can you tell me about your opening hours please?"
STANDIN_REPLY = "Yes, of course. We are open from nine in the morning until six in the evening, Monday to Saturday."


class StandinSTTService(STTService):
    """Returns a fixed transcript whenever the caller stops speaking.

    Driven by the loudness of the incoming audio rather than by VAD frames.
    Speech recognition sits UPSTREAM of the VAD in this project's pipeline
    (see voice_pipeline.py), so it cannot rely on being told when a turn
    started — the real Deepgram service has the same constraint and solves it
    the same way, with a silence threshold of its own.
    """

    def __init__(self, *, transcript: str, sample_rate: int | None = None, **kwargs):
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._transcript = transcript
        self._speaking = False
        self._silence_seconds = 0.0

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        samples = np.frombuffer(audio, dtype=np.int16)
        if samples.size == 0:
            yield None
            return

        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        chunk_seconds = samples.size / (self.sample_rate or 16000)

        if rms >= SPEECH_RMS:
            self._speaking = True
            self._silence_seconds = 0.0
            yield None
            return

        if not self._speaking:
            yield None
            return

        self._silence_seconds += chunk_seconds
        if self._silence_seconds < END_OF_TURN_SILENCE_SECONDS:
            yield None
            return

        self._speaking = False
        self._silence_seconds = 0.0
        yield TranscriptionFrame(
            text=self._transcript,
            user_id="",
            timestamp=time_now_iso8601(),
        )


class StandinLLMService(LLMService):
    """Streams back a fixed reply, word by word, after a pause.

    The pause stands in for everything a real reply waits on — the model's own
    thinking time and, in this mode, the knowledge-base lookup that is skipped
    (see app/services/rag.py). Without it every turn would answer instantly
    and the measured reply delays would describe a system nobody has.
    """

    def __init__(self, *, reply: str, think_seconds: float = 0.8, **kwargs):
        # Every settings field named explicitly, including the ones this
        # service has no use for: pipecat validates at startup that none were
        # left unset and logs an error naming each one otherwise.
        kwargs.setdefault(
            "settings",
            LLMSettings(
                model="standin",
                system_instruction=None,
                temperature=None,
                max_tokens=None,
                top_p=None,
                top_k=None,
                frequency_penalty=None,
                presence_penalty=None,
                seed=None,
                filter_incomplete_user_turns=None,
                user_turn_completion_config=None,
            ),
        )
        super().__init__(**kwargs)
        self._reply = reply
        self._think_seconds = think_seconds

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            await self._say_the_reply()
        else:
            await self.push_frame(frame, direction)

    async def _say_the_reply(self) -> None:
        # The wait comes FIRST. A real service announces the response when its
        # first token actually arrives, and TranscriptRecorder stamps
        # time_to_first_token_ms from that moment — announcing before thinking
        # would report a near-zero wait on every turn the load test saves.
        if self._think_seconds:
            await asyncio.sleep(self._think_seconds)
        await self.push_frame(LLMFullResponseStartFrame())
        # Word at a time, because a real model streams and this project's TTS
        # starts speaking on the first piece it gets. One whole-reply frame
        # would report a time-to-first-word the real system never achieves.
        words = self._reply.split(" ")
        for i, word in enumerate(words):
            piece = word if i == len(words) - 1 else word + " "
            await self.push_frame(LLMTextFrame(text=piece))
        await self.push_frame(LLMFullResponseEndFrame())


class StandinTTSService(TTSService):
    """Produces audible audio lasting about as long as the words would take
    to say.

    Audible matters: the server sends digital silence whenever the bot has
    nothing to say, so the load-test client times its replies from the first
    frame that is actually loud enough to be sound. A stand-in voice that
    generated silence would be invisible to every measurement in the test.

    A plain tone, not speech. Nobody listens to a load test, and generating
    real speech would put the very processor cost we are trying to isolate
    back into the measurement.
    """

    def __init__(self, *, sample_rate: int | None = None, first_byte_seconds: float = 0.2, **kwargs):
        # Both default to False on the base class, and every real service
        # turns them on (PiperTTSService does). Left off, nothing ever emits
        # TTSStoppedFrame, so the transport decides the bot went quiet only
        # via its own 3-second idle fallback — and BotStoppedSpeakingFrame is
        # what closes a turn here (voice_pipeline.py). Turns would merge.
        super().__init__(
            sample_rate=sample_rate,
            push_start_frame=True,
            push_stop_frames=True,
            **kwargs,
        )
        self._first_byte_seconds = first_byte_seconds

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        await self.start_tts_usage_metrics(text)
        if self._first_byte_seconds:
            await asyncio.sleep(self._first_byte_seconds)

        rate = self.sample_rate or 24000
        total_samples = max(int(len(text) / CHARS_PER_SECOND * rate), rate // 10)

        async def tone() -> AsyncIterator[bytes]:
            chunk = rate // 50  # 20ms
            for start in range(0, total_samples, chunk):
                count = min(chunk, total_samples - start)
                t = (np.arange(start, start + count) / rate).astype(np.float64)
                wave = (_TTS_AMPLITUDE * np.sin(2 * np.pi * _TTS_TONE_HZ * t)).astype(np.int16)
                yield wave.tobytes()

        async for frame in self._stream_audio_frames_from_iterator(
            tone(), in_sample_rate=rate, context_id=context_id
        ):
            await self.stop_ttfb_metrics()
            yield frame
        await self.stop_ttfb_metrics()


def announce() -> None:
    """Say plainly, in the log, that this server is not doing real work.

    Deliberately loud and repeated per call. A server left in this mode by
    accident answers real callers with a tone and a canned sentence, and the
    only thing standing between that and a silent mystery is this line.
    """
    logger.warning(
        "[STANDIN] REHEARSAL MODE — speech, model and voice are stand-ins and the "
        "knowledge base is switched off. No real provider requests are made and no "
        "caller is really being answered. Unset STANDIN_PROVIDERS to serve real calls."
    )
