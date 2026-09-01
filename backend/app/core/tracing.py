"""Task 2.7 — send per-stage AI call traces to Langfuse.

Pipecat already emits OpenTelemetry spans for every stage of a turn (STT,
LLM, TTS) with model names, token counts and durations attached. Langfuse
ingests OTLP directly, so the integration is an exporter and a couple of
credentials rather than hand-instrumenting each call — and because it is
plain OTLP, pointing this at any other OpenTelemetry backend later is a
config change, not a rewrite.

Dormant unless all three langfuse_* settings are filled in. Blank is the
default, which means no exporter is configured and pipecat's tracing stays
off entirely — zero behaviour change and zero overhead.

Must be called inside each call worker process, not just the parent: Task
2.4 gives every call its own OS process, and OpenTelemetry's tracer
provider is per-process global state that a spawned child does not inherit.
"""

import base64

from loguru import logger

from app.core.config import settings


def setup_call_tracing(conversation_id: str | None = None) -> bool:
    """Configure the OTLP exporter for this process. Returns True if tracing
    was actually enabled, so callers can pass the same answer to
    PipelineTask(enable_tracing=...) rather than guessing."""
    if not (settings.langfuse_host and settings.langfuse_public_key and settings.langfuse_secret_key):
        return False

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from pipecat.utils.tracing.setup import setup_tracing

        # Langfuse authenticates OTLP with HTTP Basic using the project key
        # pair — not a bearer token, and not a query parameter.
        auth = base64.b64encode(
            f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
        ).decode()

        exporter = OTLPSpanExporter(
            endpoint=f"{settings.langfuse_host.rstrip('/')}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {auth}"},
        )

        ok = setup_tracing(service_name="voice-agent", exporter=exporter)
        if ok:
            logger.info(f"[TRACING] Langfuse tracing enabled (conversation={conversation_id})")
        else:
            logger.warning("[TRACING] pipecat reported tracing setup as unavailable")
        return ok
    except Exception as e:
        # Observability failing must never take down a call. This is the
        # one place where swallowing an exception is clearly right: the
        # alternative is a dashboard outage ending customer conversations.
        logger.warning(f"[TRACING] Could not enable Langfuse tracing: {e}")
        return False
