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
