from datetime import UTC, datetime

from beanie import Document as BeanieDocument
from pymongo import ASCENDING, IndexModel


class Document(BeanieDocument):
    bot_id: str
    user_id: str
    # Task 5.1 — the owning organisation. user_id stays as "who created it"
    # and is no longer used for access. Blank = not yet migrated = invisible.
    org_id: str = ""
    filename: str
    chunk_count: int = 0
    created_at: datetime = datetime.now(UTC)
    # Task 2.10 — GridFS id of the original PDF, so a citation can actually
    # open the page it came from. Optional because every document uploaded
    # before this existed has no stored file: those still cite correctly,
    # they just aren't clickable. Storing the bytes (rather than only the
    # extracted chunks) is what turns "the AI said so" into something a
    # customer can verify, which is the whole point of the task.
    file_id: str | None = None

    class Settings:
        name = "documents"
        indexes = [
            IndexModel(
                [("org_id", ASCENDING), ("bot_id", ASCENDING)], name="docs_by_org_bot"
            ),
        ]
