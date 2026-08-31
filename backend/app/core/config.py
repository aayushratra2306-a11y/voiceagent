from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # MongoDB
    mongodb_url: str
    db_name: str = "voiceagent"

    # JWT
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # OpenAI
    openai_api_key: str

    # Groq — optional, faster/cheaper LLM alternative to OpenAI.
    # If set, the voice pipeline uses Groq for the LLM step instead of
    # OpenAI. Leave blank to keep using OpenAI (no other change needed —
    # this is the "switch back" the manual's task 1.2 asked for).
    groq_api_key: str = ""
    # Llama 3.3 70B (pipecat's built-in default) has been retired from Groq's
    # catalog — verified 2026-08-30 via /v1/models. gpt-oss-120b is the
    # current best tool-capable replacement (needed for function calling,
    # task 1.3): 131k context, cheap, and it's a reasoning model — see the
    # reasoning_effort="low" setting in voice_pipeline.py, which keeps it
    # fast for voice rather than letting it "think" before every reply.
    groq_model: str = "openai/gpt-oss-120b"

    # Task 1.6 — a separate, smaller/faster Groq model just for cleaning up
    # spoken RAG queries before search. Deliberately not the 120b model used
    # for actual replies: this call sits directly in the latency path of
    # every RAG lookup, so it needs to be as fast as possible, not as smart
    # as possible. Verified live against Groq's catalog 2026-08-31 (same
    # /v1/models check that caught llama-3.3-70b-versatile's retirement) —
    # ~0.5-0.7s round trip, correctly strips filler words and converts
    # spoken numbers ("page fifty" -> "page 50").
    groq_rewrite_model: str = "openai/gpt-oss-20b"

    # Task 2.1 — provider switching layer. One setting each flips the whole
    # pipeline between cloud and free/local, no code changes anywhere else
    # (see app/pipeline/providers.py). "auto" for llm_provider preserves the
    # pre-2.1 behavior exactly: Groq if a key is configured, else OpenAI.
    stt_provider: str = "deepgram"  # deepgram | whisper
    tts_provider: str = "cartesia"  # cartesia | piper
    llm_provider: str = "auto"      # auto | groq | openai

    # Task 2.2 — local speech recognition. "small" is faster-whisper's own
    # multilingual small model — verified 2026-08-31 it transcribes both
    # English and Hindi correctly on CPU (this bot's two confirmed working
    # languages), unlike the pipecat default (DISTIL_MEDIUM_EN), which is
    # English-only and would silently break Hindi callers.
    whisper_model: str = "small"

    # Deepgram
    deepgram_api_key: str

    # Cartesia
    cartesia_api_key: str

    # Pinecone
    pinecone_api_key: str
    pinecone_index_name: str = "voiceagent"
    # Task 1.8 — hybrid search. Pinecone serverless indexes are single-type
    # (dense OR sparse, never both in one index — confirmed 2026-08-31 via
    # describe_index() on the existing index), so keyword matching needs its
    # own separate index, queried alongside the dense one and merged.
    pinecone_sparse_index_name: str = "voiceagent-sparse"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
