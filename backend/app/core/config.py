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

    # Deepgram
    deepgram_api_key: str

    # Cartesia
    cartesia_api_key: str

    # Pinecone
    pinecone_api_key: str
    pinecone_index_name: str = "voiceagent"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
