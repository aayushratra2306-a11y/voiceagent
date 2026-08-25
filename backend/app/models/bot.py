from beanie import Document
from pydantic import Field
from bson import ObjectId


class Bot(Document):
    user_id: str
    name: str
    system_prompt: str = "You are a helpful voice assistant."
    voice_id: str = "a0e99841-438c-4a64-b679-ae501e7d6091"  # Cartesia default voice
    llm_model: str = "gpt-4o-mini"
    language: str = "en"

    class Settings:
        name = "bots"
