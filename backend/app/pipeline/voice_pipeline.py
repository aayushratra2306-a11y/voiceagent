import asyncio
import re
import time
import uuid
from datetime import UTC, datetime

import numpy as np
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    FunctionCallResultFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    TextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from app.core import redaction
from app.core.tracing import setup_call_tracing
from app.models.conversation import ConversationTurn
from app.pipeline import call_context
from app.pipeline.background_jobs import BACKGROUND_TOOL_RULE, BackgroundJobs
from app.pipeline.language import (
    didnt_catch_for,
    greeting_for,
    resolve_voice,
    system_language_note,
    voice_gender,
)
from app.pipeline.provider_health import ProviderHealthObserver
from app.pipeline.providers import get_llm_service, get_stt_service, get_tts_service
from app.pipeline.rag_processor import RAGContextProcessor
from app.pipeline.saga import SAGA_RULE, RequestBoundary, TurnSaga
from app.pipeline.tool_telemetry import PARTIAL_FAILURE_RULE, ToolCallTimer
from app.services.rag import query_context
from app.services.tool_registry import APPROVAL_RULE, PAYMENT_SAFETY_RULE, load_tools_for_bot


class AudioDebugger(FrameProcessor):
    """Logs every frame so we can see what types flow through the pipeline.

    The VAD lines used to be a lie, and it cost several days of misdiagnosis.
    UserStartedSpeakingFrame is emitted by the AGGREGATOR when a turn begins,
    whatever started it — VAD, or merely a transcript arriving. Logging it as
    "VAD: user started speaking" meant the logs claimed VAD was working on a
    call where it never fired once, which sent us hunting Deepgram's
    endpointing and Smart Turn's accuracy instead of the actual cause.

    VADUserStartedSpeakingFrame / VADUserStoppedSpeakingFrame are the real
    thing. They are logged separately now, and a turn that ends without VAD
    having spoken at all is called out explicitly — see _warn_if_vad_silent.
    """

    def __init__(self):
        super().__init__()
        self._vad_ever_fired = False
        self._warned_vad_silent = False

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        name = type(frame).__name__
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._vad_ever_fired = True
            logger.info("[AUDIO] VAD(real): speech detected")
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            logger.info(f"[AUDIO] VAD(real): silence detected (stop_secs={frame.stop_secs})")
        elif isinstance(frame, UserStartedSpeakingFrame):
            logger.info("[AUDIO] Turn started")
        elif isinstance(frame, UserStoppedSpeakingFrame):
            logger.info("[AUDIO] Turn stopped")
            self._warn_if_vad_silent()
        elif isinstance(frame, TranscriptionFrame):
            logger.info(f"[AUDIO] TranscriptionFrame: '{frame.text}'")
        elif any(k in name for k in ("Transcri", "STT", "Speech", "Word", "Text")):
            logger.info(f"[AUDIO] {name}: text={getattr(frame, 'text', getattr(frame, 'content', '?'))!r}")
        await self.push_frame(frame, direction)

    def _warn_if_vad_silent(self):
        """Say so, loudly and once, when turns are running without VAD.

        This is a silent degradation in pipecat, not an error, which is why
        it went unnoticed. TurnAnalyzerUserTurnStopStrategy._handle_transcription
        has a fallback for "transcripts arrive without VAD firing" whose comment
        reads: "Without VAD/turn analyzer data, assume turn is complete". It
        arms a short timer off the transcript and ends the turn — so Smart Turn
        is never consulted at all, and the turn ends wherever the STT
        endpointer happened to finalize, mid-sentence included.

        Nothing logs when that path is taken. This does.
        """
        if self._vad_ever_fired or self._warned_vad_silent:
            return
        self._warned_vad_silent = True
        logger.warning(
            "[AUDIO] Turn ended but VAD has NEVER fired on this call — pipecat is "
            "falling back to 'assume turn is complete' off the transcript alone, "
            "so Smart Turn is not running and turns will split wherever the STT "
            "endpointer finalises. Check VADParams confidence/min_volume against "
            "this caller's mic level."
        )


def _as_float(value) -> float:
    """Coerce a VAD reading to a plain float, whatever shape it arrives in.

    SileroVADAnalyzer.voice_confidence is annotated `-> float` and is not one:
    it returns `self._model(...)[0]`, a numpy value whose exact shape depends
    on the model build. A plain float() covers scalars and 0-d arrays but
    raises "only 0-dimensional arrays can be converted to Python scalars" on a
    1-element one, so the shape is flattened first rather than assumed.

    Worth the care because the failure is silent where it matters: the first
    version of this instrumentation raised on every report, pipecat turned
    that into a non-fatal ErrorFrame, and the calls carried on while producing
    no measurements at all.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(np.ravel(np.asarray(value))[0])


class MeasuredSileroVAD(SileroVADAnalyzer):
    """Silero VAD that reports what it actually measured.

    The VAD thresholds have now been changed three times by reasoning about
    symptoms — 0.7, then 0.85 to stop noise holding VAD open, then back to
    0.7 when 0.85 turned out to stop VAD firing at all. Each round cost a
    deploy and a live call to evaluate, because nothing ever logged the one
    thing that decides the outcome: the numbers Silero is actually producing
    for this caller's microphone.

    BaseVADAnalyzer.analyze_audio gates on
    ``confidence >= params.confidence and volume >= params.min_volume``, and
    computes each through a method call this class can intercept, so the real
    values cost nothing extra to observe. The periodic summary names which of
    the two gates is failing and by how much, which turns the next threshold
    change into arithmetic instead of another guess.
    """

    # Long enough that a call produces a readable handful of lines rather
    # than a flood, short enough to localise a problem to a moment.
    REPORT_EVERY_SECONDS = 5.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._peak_confidence = 0.0
        self._peak_volume = 0.0
        self._last_report = 0.0

    def voice_confidence(self, buffer) -> float:
        # float() is load-bearing, not tidiness. SileroVADAnalyzer annotates
        # this `-> float` but actually returns `self._model(...)[0]`, a numpy
        # value. Keeping it raw made the peak a numpy object too, and
        # formatting one with ":.2f" raises "unsupported format string passed
        # to numpy.ndarray.__format__" -- which is exactly what the first
        # deploy of this class did, once every report interval, so it produced
        # no measurements at all while looking like it was working.
        value = _as_float(super().voice_confidence(buffer))
        self._peak_confidence = max(self._peak_confidence, value)
        return value

    def _get_smoothed_volume(self, audio) -> float:
        # Called once per analysis window alongside voice_confidence, so this
        # is where both peaks are known and the report can be emitted.
        value = _as_float(super()._get_smoothed_volume(audio))
        self._peak_volume = max(self._peak_volume, value)
        self._report_if_due()
        return value

    def _report_if_due(self):
        now = time.monotonic()
        if now - self._last_report < self.REPORT_EVERY_SECONDS:
            return
        self._last_report = now

        peak_confidence = _as_float(self._peak_confidence)
        peak_volume = _as_float(self._peak_volume)
        confidence_ok = peak_confidence >= self._params.confidence
        volume_ok = peak_volume >= self._params.min_volume
        if confidence_ok and volume_ok:
            verdict = "would fire"
        elif not confidence_ok and not volume_ok:
            verdict = "BLOCKED by both"
        elif not confidence_ok:
            verdict = "BLOCKED by confidence"
        else:
            verdict = "BLOCKED by min_volume"

        logger.info(
            f"[VAD] peak confidence={peak_confidence:.2f} "
            f"(need {self._params.confidence}) | "
            f"peak volume={peak_volume:.2f} "
            f"(need {self._params.min_volume}) -> {verdict}"
        )
        self._peak_confidence = 0.0
        self._peak_volume = 0.0


# Root-caused 2026-08-30: LLMs default to Markdown-formatted text (tables,
# headers, bold) because that's correct for a chat screen — but this is a
# voice pipeline, and Cartesia was speaking the raw syntax out loud. User's
# own bot reply proved it verbatim: "the vertical bar you are telling me
# about" for a `|` table character, plus repeated log warnings about `|`
# and `##` not being handled cleanly by TTS's word-alignment tracking.
# This is defense #2 — the system prompt (defense #1, below) tells the model
# not to do this, but RAG-injected document content is often itself
# Markdown/tabular, and models don't always fully obey formatting
# instructions once real table data is in front of them. These substitutions
# are deliberately fragment-safe (plain character-class removals, no paired
# delimiter matching) so they work correctly no matter how the LLM's
# streaming output happens to be chunked into frames.
_MD_HEADER = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_RULE = re.compile(r"^\s*[-=*_]{3,}\s*$", re.MULTILINE)
_MD_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)  # leading "- ", "* ", "+ "
_MD_TABLE_ROW_EDGES = re.compile(r"^\s*\|\s*|\s*\|\s*$", re.MULTILINE)  # leading/trailing |
_MD_WHITESPACE = re.compile(r"[ \t]{2,}")
_MD_BLANK_LINES = re.compile(r"\n{2,}")


def strip_markdown_for_speech(text: str) -> str:
    """Remove Markdown syntax that TTS engines otherwise speak literally."""
    text = _MD_TABLE_SEP.sub(" ", text)  # |---|---| separator rows, before pipe strip
    text = _MD_RULE.sub(" ", text)  # standalone --- / *** / === rules
    text = _MD_HEADER.sub("", text)  # leading #, ##, ### ...
    text = _MD_BULLET.sub("", text)  # leading bullet markers
    text = _MD_TABLE_ROW_EDGES.sub("", text)  # drop the | at each row's own edges first,
    text = text.replace("|", ", ")  # so what's left are only *interior* cell separators
    text = text.replace("**", "").replace("__", "")  # bold
    text = re.sub(r"(?<!\w)\*(?!\s)([^*]*?)\*(?!\w)", r"\1", text)  # *italic*
    text = text.replace("`", "")  # inline code / code fences
    text = re.sub(r"^>\s*", "", text, flags=re.MULTILINE)  # blockquotes
    text = _MD_WHITESPACE.sub(" ", text)
    text = _MD_BLANK_LINES.sub(" ", text)
    # Deliberately NOT calling .strip() here. Pipecat's LLM frames carry
    # their own leading/trailing spacing on purpose so consecutive streamed
    # fragments join into correctly-spaced words/sentences
    # (LLMTextFrame.includes_inter_frame_spaces = True in pipecat's source).
    # .strip()-ing every fragment ate exactly that boundary spacing —
    # confirmed via live session log 2026-08-30: replies came out as
    # "SureIndiaisavastanddiversecountry...", every space gone. Only trim the
    # small, safe amount this function itself might introduce (e.g. the " "
    # a fully-stripped rule/table line collapses to), never the original
    # fragment's own edges.
    return text


class MarkdownStripper(FrameProcessor):
    """Sits between the LLM and TTS. Cleans Markdown syntax out of streamed
    text before it reaches TTS, so tables/headers/bold don't get spoken as
    literal symbols ("vertical bar", "hash hash", "dash dash dash...")."""
    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TextFrame) and frame.text:
            frame.text = strip_markdown_for_speech(frame.text)
        await self.push_frame(frame, direction)


class TranscriptRecorder(FrameProcessor):
    """Task 1.5 — saves every completed turn to MongoDB, with per-stage
    timing captured live as it happens (not reconstructed afterward), so a
    future "it feels slow" question is a query, not the manual log-timestamp
    hunt this exact session needed today to find the ~1.5s Groq/RAG gap.

    Positioned after MarkdownStripper, before tts — the one spot that sees
    everything in a single pass without needing two processors:
      - TranscriptionFrame / UserStoppedSpeakingFrame: these originate
        upstream (STT / the turn aggregator) and flow downstream through
        here on their way into the LLM, same as into any other processor
        positioned after them.
      - LLMFullResponseStartFrame / TextFrame / FunctionCallResultFrame:
        originate at `llm` and flow straight downstream to here.
      - BotStartedSpeakingFrame / BotStoppedSpeakingFrame: originate at
        transport.output(), further downstream still — but these are
        broadcast frames (pushed both directions), so they propagate back
        upstream through tts and reach this position too.
    Sits after MarkdownStripper specifically so the saved assistant_reply is
    the actual cleaned text that got spoken, not the raw Markdown.
    """

    def __init__(
        self, session_id: str, bot_id: str | None, bot_name: str,
        redaction_kinds: list[str] | None = None,
    ):
        super().__init__()
        self._session_id = session_id
        self._bot_id = bot_id
        self._bot_name = bot_name
        # Task 6.2. None means "not configured, or explicitly cleared" —
        # ALL_KINDS is the safe interpretation of that (see this
        # constructor's caller for why), never an empty set arrived at by
        # accident.
        self._redaction_kinds = frozenset(redaction_kinds) if redaction_kinds is not None \
            else redaction.ALL_KINDS
        self._reset_turn()

    def _reset_turn(self):
        self._user_transcript = ""
        self._assistant_parts: list[str] = []
        self._tool_calls: list[dict] = []
        self._user_stopped_at: datetime | None = None
        self._llm_first_response_at: datetime | None = None
        self._bot_started_speaking_at: datetime | None = None

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStoppedSpeakingFrame):
            if self._user_stopped_at is None:
                self._user_stopped_at = datetime.now(UTC)
        elif isinstance(frame, TranscriptionFrame) and frame.text:
            self._user_transcript = frame.text
        elif isinstance(frame, LLMFullResponseStartFrame):
            if self._llm_first_response_at is None:
                self._llm_first_response_at = datetime.now(UTC)
        elif isinstance(frame, FunctionCallResultFrame):
            self._tool_calls.append({
                "name": frame.function_name,
                "arguments": frame.arguments,
                "result": frame.result,
            })
        elif isinstance(frame, (TextFrame, TTSSpeakFrame)) and getattr(frame, "text", None):
            # Covers both normal LLM replies and the greeting/fallback
            # TTSSpeakFrame injections, which are not TextFrame subclasses.
            self._assistant_parts.append(frame.text)
        elif isinstance(frame, BotStartedSpeakingFrame):
            if self._bot_started_speaking_at is None:
                self._bot_started_speaking_at = datetime.now(UTC)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            await self._finalize_turn()

        await self.push_frame(frame, direction)

    async def _finalize_turn(self):
        # Nothing real happened — e.g. a stray bot-speaking event with no
        # content either side. Don't write empty noise to the database.
        if not self._user_transcript and not self._assistant_parts:
            self._reset_turn()
            return

        # Task 6.2 — redacted HERE, before a ConversationTurn is even built,
        # not after insert(). "Redact before it touches disk, not
        # afterwards" is the manual's own framing, and this is the one
        # place in the whole pipeline where the complete turn exists in
        # memory before it exists in the database.
        #
        # Tool call arguments/results are walked too: a customer's own
        # "take a payment" tool can carry a card number in its arguments,
        # and that record is stored on the turn exactly like the
        # transcript is (task 3.1's `FunctionCallResultFrame` handling
        # above stores it whole).
        user_redacted = redaction.redact(self._user_transcript, self._redaction_kinds)
        assistant_redacted = redaction.redact(
            "".join(self._assistant_parts), self._redaction_kinds
        )
        tool_calls, tool_redaction_kinds = redaction.redact_structure(
            self._tool_calls, self._redaction_kinds
        )
        redacted_kinds = set(user_redacted.kinds) | set(assistant_redacted.kinds) \
            | set(tool_redaction_kinds)

        turn = ConversationTurn(
            session_id=self._session_id,
            bot_id=self._bot_id,
            bot_name=self._bot_name,
            user_transcript=user_redacted.text,
            assistant_reply=assistant_redacted.text,
            tool_calls=tool_calls,
            redacted_kinds=sorted(redacted_kinds),
            user_stopped_speaking_at=self._user_stopped_at,
            llm_first_response_at=self._llm_first_response_at,
            bot_started_speaking_at=self._bot_started_speaking_at,
            bot_stopped_speaking_at=datetime.now(UTC),
        )
        if self._user_stopped_at:
            if self._llm_first_response_at:
                turn.time_to_first_token_ms = int(
                    (self._llm_first_response_at - self._user_stopped_at).total_seconds() * 1000
                )
            if self._bot_started_speaking_at:
                turn.time_to_speech_ms = int(
                    (self._bot_started_speaking_at - self._user_stopped_at).total_seconds() * 1000
                )

        try:
            await turn.insert()
            logger.info(
                f"[TRANSCRIPT] Saved turn — "
                f"time_to_first_token_ms={turn.time_to_first_token_ms}, "
                f"time_to_speech_ms={turn.time_to_speech_ms}, "
                f"tools={[t['name'] for t in self._tool_calls]}"
            )
        except Exception as e:
            # A transcript-saving failure must never take down the actual
            # conversation — log it and move on.
            logger.warning(f"[TRANSCRIPT] Failed to save turn: {e}")

        self._reset_turn()


async def run_voice_pipeline(
    webrtc_connection: SmallWebRTCConnection,
    bot_name: str,
    system_prompt: str,
    voice_id: str,
    llm_model: str,
    language: str = "en",
    bot_id: str | None = None,
    # Task 3.7 — pc_id is the same id app.api.connect keys its live-call
    # registry by, set by call_worker.py once the WebRTC handler has decided
    # it (not known before this call's process even starts). payment_queue
    # is the parent-to-child channel a payment webhook uses to speak back
    # into this specific call — see _forward_payments below, the payment
    # equivalent of call_worker.py's existing `_forward_ice`. Both default
    # to None so a bot with no payment tool, or a call started before this
    # task, works exactly as before.
    pc_id: str | None = None,
    payment_queue=None,
    # Task 3.8 — the bot's OWNER (Bot.user_id), not the caller. A webhook
    # fires to whichever customer of this platform configured it, so their
    # own system hears about their own bot's events.
    user_id: str | None = None,
    # Task 6.2 — which sensitive-data categories to mask out of this bot's
    # transcripts before they are stored (app/core/redaction.py). None
    # rather than a mutable default list, and treated as "everything" when
    # None reaches TranscriptRecorder below — the safe default for a call
    # started before this field existed on an older bot_config, or with the
    # field explicitly cleared, is full redaction, not none.
    redact_transcripts: list[str] | None = None,
):
    session_id = str(uuid.uuid4())
    call_started_at = datetime.now(UTC)  # task 3.8 — call.ended's duration
    logger.info(f"[PIPELINE] Starting for bot: {bot_name} (session {session_id})")

    # Task 3.5 — the built-in tools are plain module-level functions, so they
    # get no bot and no session, only the arguments the model supplied. The
    # booking template needs the bot's time zone, and it must NOT be
    # something the model can pass in. This is where it comes from; see
    # call_context.py for why a module-level value is safe here (one OS
    # process per call). pc_id is here for the same reason — task 3.7's
    # payment tool stamps it onto the PaymentSession it creates.
    call_context.set_call(
        bot_id=bot_id, session_id=session_id, language=language, pc_id=pc_id, user_id=user_id
    )

    # NOTE (Phase 1, task 1.1): pipecat 1.7.0 removed `vad_analyzer` from
    # TransportParams entirely. Passing it here is silently dropped by
    # pydantic's default extra="ignore" behaviour — it was never actually
    # wired in, despite all the earlier VAD threshold tuning. Confirmed by
    # direct inspection of the installed 1.7.0 source (TransportParams has
    # no such field; hasattr() on a constructed instance returns False).
    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    )

    # endpointing/interim_results explicit rather than left at Deepgram's
    # undocumented default. Root-caused via live session logs on 2026-08-30:
    # an InterimTranscriptionFrame arrived fine, but no final TranscriptionFrame
    # ever followed — our turn-stop logic only reads finalized text (see
    # TurnAnalyzerUserTurnStopStrategy._handle_transcription), so the turn sat
    # open until the 5s fallback closed it. Traced through pipecat's Deepgram
    # client: it only reacts to message.is_final (driven by `endpointing`,
    # a silence-duration threshold) — it does NOT handle Deepgram's separate
    # UtteranceEnd event, so `utterance_end_ms` would be a no-op here even
    # though Deepgram's own docs suggest it for noisy environments.
    # 300ms is a standard middle-ground for voice agents — long enough to not
    # cut off a mid-sentence breath, short enough not to sit waiting.
    # (Task 2.1: STT/LLM/TTS construction now goes through the provider
    # factory in app/pipeline/providers.py — settings.stt_provider etc.
    # switch cloud vs. local with no code change here. The endpointing=300
    # reasoning above still applies; it lives inside get_stt_service() now.)
    # Resolve the voice up front rather than letting get_tts_service do it
    # privately: what the bot SAYS has to agree with the gender of the voice
    # it says it in, so the greeting, the fallback and the system prompt all
    # need to know which voice actually won. resolve_voice is idempotent, so
    # get_tts_service resolving again below is harmless.
    voice_id = resolve_voice(voice_id, language)
    speaking_gender = voice_gender(voice_id, language)

    stt = get_stt_service(language=language)
    llm = get_llm_service(llm_model=llm_model)
    tts = get_tts_service(voice_id=voice_id, language=language)

    # Task 3.2 — per-tool timing. Pipecat already runs a turn's calls
    # concurrently; this is how you find out which of them was the slow one,
    # and it logs the count so "did the model actually use two tools at once"
    # is answerable from a log rather than by inference.
    tool_timer = ToolCallTimer()

    # Task 3.3 / 3.4 — both are created here, before the tools that close
    # over them and before the handler below that drives them.
    jobs = BackgroundJobs()

    async def _announce_rollback(sentence: str) -> None:
        """Put a rollback summary into the conversation for the model to say."""
        await task.queue_frame(
            LLMMessagesAppendFrame(messages=[{"role": "system", "content": sentence}], run_llm=True)
        )

    saga = TurnSaga(announce=_announce_rollback)

    @llm.event_handler("on_function_calls_started")
    async def _on_function_calls_started(service, function_calls):
        # Tells the saga how many results to expect: pipecat announces a
        # batch starting but never that it finished, so completion is
        # counted from this number.
        saga.begin(len(function_calls))
        await tool_timer.on_calls_started(service, function_calls)

    # Defense #1 against the Markdown-in-speech problem: tell the model
    # outright not to do it. Applied to every bot regardless of what the
    # user wrote in their own prompt, and passed to RAGContextProcessor
    # below too, since it rebuilds the system message on every turn.
    voice_system_prompt = (
        f"{system_prompt}\n\n"
        "This is a real-time VOICE conversation, not a text chat — the caller "
        "only hears your words spoken aloud, they never see any text on a "
        "screen. Never use Markdown formatting: no headers (#), no bold/italic "
        "(**/*), no tables (|), no bullet points, no horizontal rules (---), "
        "no code blocks. Speak in plain, natural spoken sentences and "
        "paragraphs, the way a person would say it out loud."
        # The bot's language reached STT and TTS but never the model itself,
        # so a Hindi bot heard Hindi and could speak Hindi while still
        # THINKING in English — and duly answered a Hindi question in
        # English until the caller explicitly asked it to switch. Confirmed
        # live 2026-09-04. Appended after the Markdown rules, not before, so
        # it sits closest to the turn and is the last thing the model reads.
        + system_language_note(language, speaking_gender)
        # Task 3.2. Pipecat runs a turn's tool calls concurrently, which makes
        # a mixed outcome normal rather than rare: one booked, one failed.
        # Left to itself the model summarises the batch as a single result,
        # and the manual is blunt about why that matters — "all done" when the
        # text message failed is how a customer ends up believing they have a
        # cab they do not have.
        + PARTIAL_FAILURE_RULE
    )

    # Task 1.3: pass the tool functions straight into `tools=` — pipecat 1.7.0
    # auto-extracts each one's name/description/parameter schema from its
    # type hints and docstring, and auto-registers the handler since these
    # are plain async functions (a "direct function" in pipecat's terms).
    # No manual FunctionSchema or register_function call needed.
    #
    # Task 3.1: which tools, though, is now this bot's own configuration
    # rather than one global list. load_tools_for_bot returns a mix pipecat
    # accepts as-is — plain functions for the built-ins, FunctionSchema
    # objects carrying a generated handler for tools defined in the
    # database. A bot with nothing configured still gets the built-ins, so
    # every bot that predates this keeps working unchanged.
    #
    # Loaded per call rather than cached: a customer editing a tool expects
    # their next call to use it, and this costs one indexed query against a
    # handful of rows while the greeting is still playing.
    # Task 3.3 — created before the tools, because a long-running tool's
    # handler closes over it. The pipeline task does not exist yet; it is
    # attached below, once it does.
    tools, has_background, has_undo, has_payment, has_approval = await load_tools_for_bot(
        bot_id, jobs, saga
    )
    if has_undo:
        voice_system_prompt += SAGA_RULE
    if has_background:
        # Only when this bot actually has such a tool. Added to
        # voice_system_prompt itself rather than to the context alone,
        # because RAGContextProcessor rebuilds messages[0] from that string
        # on every turn and would otherwise drop it after the first.
        voice_system_prompt += BACKGROUND_TOOL_RULE
    if has_payment:
        # Task 3.7. The manual's tip is direct: never take card numbers by
        # voice, always send a link — the compliance burden of handling
        # card data directly is enormous and completely avoidable.
        voice_system_prompt += PAYMENT_SAFETY_RULE
    if has_approval:
        # Task 3.10 — no company will let an AI approve a large refund
        # unsupervised; this is what tells the model the difference between
        # a normal tool result and one still waiting on a person.
        voice_system_prompt += APPROVAL_RULE
    context = LLMContext(
        messages=[{"role": "system", "content": voice_system_prompt}],
        tools=tools,
    )

    # In 1.7.0, VAD + end-of-turn detection live on the user context
    # aggregator, not the transport. Passing `vad_analyzer` here wires up:
    #   1. Real Silero VAD
    #   2. Smart Turn v3 as the end-of-turn decider (pipecat's built-in default
    #      stop strategy — no extra config needed, just installed via
    #      `pip install "pipecat-ai[local-smart-turn]"`)
    #   3. Interruptions — `enable_interruptions=True` is the default on the
    #      VAD start strategy, so the bot already stops talking when spoken over.
    # stop_secs=0.5 (raised from 0.2 on 2026-08-31): live testing showed a
    # real turn-splitting bug at 0.2s — a self-correction like "page forty...
    # no wait, twenty" got cut into TWO separate finalized turns on the brief
    # pause mid-correction (confirmed via logs: two TranscriptionFrames 2ms
    # apart, 'Page forty.' then 'Two zero. Page', each independently
    # triggering its own RAG lookup + reply — the bot answered the
    # pre-correction fragment before the real question ever completed).
    # This isn't a hearing/accuracy problem — Deepgram transcribed both
    # fragments correctly — it's VAD declaring "done talking" too eagerly
    # during a natural mid-thought pause, before Smart Turn's own classifier
    # gets enough context to call it incomplete. 0.5s gives a real pause room
    # to exist without users noticing added latency (turn-taking research
    # generally puts the "feels slow" threshold well above this).
    #
    # confidence/min_volume: mic-specific, tuned live against real hardware —
    # do not "fix" these back to a textbook default without testing first.
    #   0.25 (original)          -> too permissive: caught background noise
    #                                as continuous speech, VAD never cleanly
    #                                reported "stopped" (task 1.1 symptom).
    #   0.6  (pipecat's default) -> too strict for this mic: normal speaking
    #                                volume didn't clear the bar, needed to
    #                                near-shout to register at all.
    #   0.4  (current)           -> splitting the difference. If speech still
    #                                isn't registering, lower toward 0.3; if
    #                                background noise triggers it again,
    #                                raise toward 0.5. One VAD_MIN_VOLUME
    #                                works for pipecat's test setup, not
    #                                necessarily for any given mic/room —
    #                                this needs live iteration, not a formula.
    #
    # confidence=0.85 (raised from 0.7, 2026-08-30): live testing at
    # min_volume=0.4 showed VAD getting stuck reporting "still speaking" for
    # 5+ seconds after the person actually stopped — even though Deepgram
    # finalized the transcript correctly and fast in the same window. That
    # points at background noise (loud enough to pass min_volume, but not
    # actually speech) continuously resetting VAD's stop timer. confidence
    # is Silero's own ML speech-probability score — the right lever for
    # "is this actually speech" as opposed to min_volume's raw loudness gate,
    # which can't tell noise from speech at all. Raising confidence alone
    # first, leaving min_volume at 0.4, to isolate which one actually fixes
    # it rather than changing both blind.
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # confidence 0.85 -> 0.7, reverting the 2026-08-30 raise, because
            # 0.85 overshot into a worse failure than the one it fixed.
            #
            # Live call 2026-09-04: VAD did not fire ONCE. Every turn began
            # with "strategy: TranscriptionUserTurnStartStrategy" and no
            # analyze_end_of_turn line appeared anywhere in the log, meaning
            # Smart Turn never ran. Pipecat does not error on that; it falls
            # back (TurnAnalyzerUserTurnStopStrategy._handle_transcription,
            # "Without VAD/turn analyzer data, assume turn is complete") to
            # arming a timer off the transcript instead.
            #
            # The arithmetic confirms it exactly: that fallback waits
            # `_stt_timeout - _stop_secs`, and `_stop_secs` is still 0.0
            # because it is only ever set from a VAD stop frame. 0.7 - 0.0 =
            # 0.7s after the final transcript at 10:03:02.120 gives
            # 10:03:02.820; the turn stopped at 10:03:02.821. So the turn
            # ended wherever Deepgram happened to finalise — mid-sentence,
            # while the caller was still speaking — and the second half became
            # its own turn. That is the whole "lost words" bug, and it is a
            # VAD sensitivity problem, not the STT endpointing one we assumed.
            #
            # Raising confidence to 0.85 was meant to stop background noise
            # holding VAD open ("still speaking" for 5+ seconds). Not firing
            # at all is the worse of the two failures by a distance: a stuck
            # VAD delays a reply, whereas a silent VAD disables Smart Turn
            # entirely and truncates what the caller actually said. 0.7 is the
            # value that was in place through the earlier sessions where Smart
            # Turn demonstrably did run.
            #
            # If noise-holding returns, raise min_volume (the loudness gate)
            # before touching confidence again — and check the new
            # "VAD(real)" / "VAD has NEVER fired" log lines to tell the two
            # failure modes apart, which the old logging could not do.
            vad_analyzer=MeasuredSileroVAD(params=VADParams(
                confidence=0.6,
                min_volume=0.4,
                start_secs=0.2,
                stop_secs=0.5,
            )),
        ),
    )

    user_aggregator = context_aggregator.user()

    # Task 3.4 — RequestBoundary resets the saga when the CALLER speaks, which
    # is what bounds a rollback now that its scope is the request rather than
    # one batch of tool calls. Placed beside AudioDebugger, which is where the
    # same turn frames are already being watched.
    pipeline_steps = [
        transport.input(), stt, AudioDebugger(), RequestBoundary(saga), user_aggregator,
    ]

    # Bug found 2026-09-03: this used to sit BEFORE user_aggregator and react
    # to every raw TranscriptionFrame straight from Deepgram. Deepgram closes
    # a "final" chunk out on any pause past its endpointing threshold, and
    # pipecat pushes each one downstream unconditionally with no merging —
    # so one spoken sentence with a mid-thought pause could search on half a
    # sentence, then search again for the other half. user_aggregator already
    # solves this correctly: it buffers fragments and only commits real text
    # once pipecat's own turn-detector (VAD + Smart Turn) decides the user is
    # actually done. Moving this processor to AFTER the aggregator, keyed off
    # the LLMContextFrame it emits at that point, means it only ever fires
    # once per real turn, on whatever text the LLM is about to see — never
    # less. See rag_processor.py's latest_user_text() docstring for the full
    # story, including the separate, genuine limitation this does NOT fix.
    if bot_id:
        # webrtc_connection is passed so the processor can publish Task 2.10
        # source citations straight to the browser over the data channel.
        pipeline_steps.append(
            RAGContextProcessor(bot_id, context, voice_system_prompt, webrtc_connection)
        )

    pipeline_steps += [
        llm,
        MarkdownStripper(),
        TranscriptRecorder(
            session_id=session_id, bot_id=bot_id, bot_name=bot_name,
            redaction_kinds=redact_transcripts,
        ),
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ]

    pipeline = Pipeline(pipeline_steps)

    # Task 2.7: enable_metrics/enable_usage_metrics turn on pipecat's
    # built-in per-stage timing (TTFB per service, etc.) — pipecat's own
    # CLI-generated bot templates set these too.
    #
    # audio_in_enabled/audio_out_enabled used to be passed here instead —
    # found and fixed while adding the line above: PipelineParams has no
    # such fields (they belong to TransportParams, already set correctly
    # on the transport itself, above), so pydantic's default extra="ignore"
    # was silently discarding both — the exact same dead-config pattern
    # Task 1.1 found on VAD. Harmless here (the transport's own
    # TransportParams already does the real job), but worth removing
    # rather than leaving misleading no-op code in place.
    # Task 2.7 — set up before the task is built, because enable_tracing is
    # decided at construction. Returns False (and stays completely inert)
    # unless the langfuse_* settings are filled in, so the default build is
    # unchanged. Done here rather than at import: each call is its own
    # process, and OpenTelemetry's provider is per-process global state.
    tracing_enabled = setup_call_tracing(conversation_id=session_id)

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        # Task 4.6 — watches for a provider failing or recovering and tells
        # its circuit breaker. An observer rather than a processor because
        # a service pushes its error frames UPSTREAM, where a processor
        # placed after it would never see them. See provider_health.py.
        observers=[ProviderHealthObserver()],
        enable_tracing=tracing_enabled,
        # Ties every span from this call together under one trace, so the
        # dashboard shows a conversation rather than loose per-stage spans.
        conversation_id=session_id,
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("[PIPELINE] Client connected — sending greeting")
        await task.queue_frame(TTSSpeakFrame(greeting_for(language, speaking_gender)))

    # Task 3.3 — a finished background job speaks into this task.
    jobs.attach(task)

    # Task 3.7 — the payment equivalent of call_worker.py's `_forward_ice`:
    # a payment webhook lands in the PARENT process, this call runs in its
    # own child process (task 2.4), so the only way across is the queue the
    # parent was handed at spawn time. Reading it blocks a thread, not the
    # event loop, same as ice's forwarder.
    payment_forward_task = None
    if payment_queue is not None:
        async def _forward_payments() -> None:
            loop = asyncio.get_event_loop()
            while True:
                item = await loop.run_in_executor(None, payment_queue.get)
                if item is None:  # sentinel: parent is done sending
                    return
                status = item.get("status")
                note = (
                    f"A payment update just arrived from the payment provider for "
                    f"reference {item.get('reference')}"
                    + (f", amount {item['amount']}" if item.get("amount") else "")
                    + f": status is '{status}'. "
                    + (
                        "It has been PAID. Tell the caller this now, clearly, as an "
                        "interruption to whatever is currently being discussed."
                        if status == "paid"
                        else "It did NOT succeed. Tell the caller plainly and ask if "
                        "they would like the link sent again — do not say it was paid."
                    )
                )
                await jobs.announce_external(note)

        payment_forward_task = asyncio.create_task(_forward_payments())

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("[PIPELINE] Client disconnected")
        # Deliberately before task.cancel(): jobs still running are not
        # cancelled (cancelling a request that may already have booked
        # something is how state becomes unknowable) — they finish and log
        # themselves, which is the only record that they happened.
        jobs.shutdown()
        if payment_forward_task is not None:
            payment_forward_task.cancel()

        # Task 3.8 — the manual's own flagship example event. Wrapped so a
        # webhook-subsystem failure can never delay or break the actual
        # teardown below it; emit() itself only queues a durable row (see
        # services/webhooks.py) so this is a fast insert, not a network call.
        try:
            from app.services.webhooks import emit

            await emit(
                "call.ended",
                user_id=user_id,
                payload={
                    "bot_id": bot_id,
                    "bot_name": bot_name,
                    "session_id": session_id,
                    "started_at": call_started_at.isoformat(),
                    "ended_at": datetime.now(UTC).isoformat(),
                    "duration_seconds": (datetime.now(UTC) - call_started_at).total_seconds(),
                },
            )
        except Exception as e:
            logger.warning(f"[PIPELINE] Could not queue call.ended: {type(e).__name__}: {e}")

        await task.cancel()

    # Fires when a user turn stays open ~5s with no completed transcript —
    # e.g. VAD/Smart Turn caught something acoustic (noise, a breath, a mic
    # bump) but Deepgram never returned any transcript for it. Without this,
    # that case was silent dead air with no indication anything happened —
    # exactly the "just listens forever" symptom found during Phase 1 testing
    # on 2026-08-30. Confirmed via source trace to
    # UserTurnController._user_turn_stop_timeout_task_handler (fires
    # on_user_turn_stop_timeout, then force-stops the turn with strategy=None).
    @user_aggregator.event_handler("on_user_turn_stop_timeout")
    async def on_user_turn_stop_timeout(aggregator):
        logger.warning("[PIPELINE] User turn timed out with no transcript — prompting caller to repeat")
        await task.queue_frame(TTSSpeakFrame(didnt_catch_for(language, speaking_gender)))

    # Latency (2026-09-03). Task 2.4 gives every call a fresh process, so a
    # worker starts with NO open connections to anything. The first retrieval
    # of a call therefore pays a cold TLS handshake to OpenAI, to both
    # Pinecone indexes, and to the reranker, one after another. Measured on
    # the server: the first lookup in a call took 7.0s and 11.0s, while a
    # later lookup in the same call took 3.4s. That gap is almost entirely
    # connection setup, and the caller pays it on their very first question.
    #
    # The greeting takes two to three seconds to speak and nothing else is
    # happening during it, so the handshakes are done there instead. By the
    # time anyone finishes their first sentence the sockets are open.
    #
    # Fire-and-forget on purpose: it must never delay the pipeline starting,
    # and a failure here is a missed optimisation, not a broken call.
    if bot_id:
        async def _warm_retrieval():
            try:
                t = time.perf_counter()
                await query_context(bot_id, "warm up")
                logger.info(f"[PIPELINE] Retrieval connections warmed in {time.perf_counter() - t:.2f}s")
            except Exception as e:
                logger.debug(f"[PIPELINE] Retrieval warm-up skipped: {e}")

        asyncio.create_task(_warm_retrieval())

    logger.info("[PIPELINE] Running…")
    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
