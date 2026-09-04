import asyncio
import re
import time
import uuid
from datetime import UTC, datetime

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    FunctionCallResultFrame,
    LLMFullResponseStartFrame,
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

from app.core.tracing import setup_call_tracing
from app.models.conversation import ConversationTurn
from app.pipeline.language import (
    didnt_catch_for,
    greeting_for,
    resolve_voice,
    system_language_note,
    voice_gender,
)
from app.pipeline.providers import get_llm_service, get_stt_service, get_tts_service
from app.pipeline.rag_processor import RAGContextProcessor
from app.pipeline.tools import TOOLS
from app.services.rag import query_context


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

    def __init__(self, session_id: str, bot_id: str | None, bot_name: str):
        super().__init__()
        self._session_id = session_id
        self._bot_id = bot_id
        self._bot_name = bot_name
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

        turn = ConversationTurn(
            session_id=self._session_id,
            bot_id=self._bot_id,
            bot_name=self._bot_name,
            user_transcript=self._user_transcript,
            assistant_reply="".join(self._assistant_parts),
            tool_calls=self._tool_calls,
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
):
    session_id = str(uuid.uuid4())
    logger.info(f"[PIPELINE] Starting for bot: {bot_name} (session {session_id})")

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
    )

    # Task 1.3: pass the tool functions straight into `tools=` — pipecat 1.7.0
    # auto-extracts each one's name/description/parameter schema from its
    # type hints and docstring, and auto-registers the handler since these
    # are plain async functions (a "direct function" in pipecat's terms).
    # No manual FunctionSchema or register_function call needed.
    context = LLMContext(
        messages=[{"role": "system", "content": voice_system_prompt}],
        tools=TOOLS,
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
            vad_analyzer=SileroVADAnalyzer(params=VADParams(
                confidence=0.7,
                min_volume=0.4,
                start_secs=0.2,
                stop_secs=0.5,
            )),
        ),
    )

    user_aggregator = context_aggregator.user()

    pipeline_steps = [transport.input(), stt, AudioDebugger(), user_aggregator]

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
        TranscriptRecorder(session_id=session_id, bot_id=bot_id, bot_name=bot_name),
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
        enable_tracing=tracing_enabled,
        # Ties every span from this call together under one trace, so the
        # dashboard shows a conversation rather than loose per-stage spans.
        conversation_id=session_id,
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("[PIPELINE] Client connected — sending greeting")
        await task.queue_frame(TTSSpeakFrame(greeting_for(language, speaking_gender)))

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("[PIPELINE] Client disconnected")
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
