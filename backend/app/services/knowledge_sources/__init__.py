"""Task 7.5 — one fetcher per external knowledge source.

Each module here exposes an async function returning a list of
`FetchedItem` — a common shape (`external_id`, `url`, `title`, `text`) that
app/services/knowledge_sync.py diffs against what is already stored,
regardless of which kind of source produced it. Adding a new source kind
later means adding one more module here and one branch in
knowledge_sync.py's dispatch, not touching the diffing logic itself.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FetchedItem:
    # Stable identity WITHIN this source — a normalized URL, a Notion page
    # id, a Drive file id. This is the join key a re-sync uses to recognise
    # "this is the same item, re-fetched" rather than a new one.
    external_id: str
    url: str
    title: str
    text: str
