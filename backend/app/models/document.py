from datetime import UTC, datetime

from beanie import Document as BeanieDocument


class Document(BeanieDocument):
    bot_id: str
    user_id: str
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

    # --- task 7.5, synced (not manually uploaded) knowledge sources --------
    # "pdf" for everything task 2.10 already handles (the default, so every
    # Document row that predates this feature reads correctly with no
    # migration). A page pulled from a website/Notion/Drive sync instead of
    # a manual upload sets this to say where it actually came from.
    source_kind: str = "pdf"
    # Which KnowledgeSource (below) this Document was produced by, and which
    # item WITHIN that source it is — a URL, a Notion page id, a Drive file
    # id. Both None for a manually uploaded PDF. This pair is what lets a
    # re-sync tell "this is the same page, re-fetched" from "this is a new
    # page" without ever re-uploading anything by hand — it is the actual
    # join key a sync uses to find the Document it created last time.
    source_id: str | None = None
    external_id: str | None = None
    # Where a citation for this content actually points, when it isn't a
    # stored PDF — the live URL of the page/file it came from.
    source_url: str | None = None
    # SHA-256 of the extracted text as of the last successful sync. The
    # manual's own tip: "only re-index what actually changed... re-embedding
    # an entire large document set on every sync is slow and expensive, and
    # it will become your biggest recurring cost surprisingly quickly." This
    # is the whole mechanism that makes that true — a sync compares this
    # against the freshly extracted text's hash and skips re-embedding
    # anything that has not actually changed.
    content_hash: str | None = None

    class Settings:
        name = "documents"
        indexes = ["bot_id", "source_id"]
