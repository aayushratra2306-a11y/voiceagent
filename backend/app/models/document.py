from datetime import UTC, datetime

from beanie import Document as BeanieDocument


class Document(BeanieDocument):
    bot_id: str
    user_id: str
    filename: str
    chunk_count: int = 0
    created_at: datetime = datetime.now(UTC)

    class Settings:
        name = "documents"
