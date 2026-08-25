from datetime import datetime, timezone
from beanie import Document as BeanieDocument


class Document(BeanieDocument):
    bot_id: str
    user_id: str
    filename: str
    chunk_count: int = 0
    created_at: datetime = datetime.now(timezone.utc)

    class Settings:
        name = "documents"
