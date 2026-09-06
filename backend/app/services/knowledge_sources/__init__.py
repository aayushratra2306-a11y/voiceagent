"""Task 7.5 — one fetcher per external knowledge source.

Each module here exposes an async function returning a `FetchResult` — a
common shape (a list of `FetchedItem`, plus whether the source was seen in
full) that app/services/knowledge_sync.py diffs against what is already
stored, regardless of which kind of source produced it. Adding a new source
kind later means adding one more module here and one branch in
knowledge_sync.py's dispatch, not touching the diffing logic itself.

Every fetcher here keeps the same contract: **it never raises.** One
customer's expired Notion token or briefly-unreachable website must not
take down the sync loop that is also responsible for every other
customer's sources.

`FetchResult.complete` is what makes that contract safe rather than
dangerous, and it exists because the first version of this module did not
have it. See its own comment below.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FetchedItem:
    # Stable identity WITHIN this source — a normalized URL, a Notion page
    # id, a Drive file id. This is the join key a re-sync uses to recognise
    # "this is the same item, re-fetched" rather than a new one.
    external_id: str
    url: str
    title: str
    text: str


@dataclass(frozen=True)
class FetchResult:
    """What one fetch of one source actually saw.

    `complete` answers the only question the diffing logic cannot answer
    for itself: **is an item's absence from `items` evidence that it was
    deleted at the source, or evidence that this fetch failed?** Those two
    look identical from the outside and mean opposite things.

    This is not hypothetical tidiness. Every fetcher here promises never to
    raise, so a website being down for thirty seconds, an expired Notion
    token, or a Drive key losing folder access all came back as a perfectly
    ordinary empty list — and knowledge_sync.py, having nothing else to go
    on, correctly concluded that every page had been deleted at the source
    and removed the customer's ENTIRE synced knowledge base, Pinecone
    vectors and all, then recorded the sync as `"ok"`. Reproduced directly
    against a real database before this field existed: three documents in,
    zero documents out, status "ok", no error recorded anywhere.

    So: `complete=True` means this fetch saw the whole source and an
    absence really is a deletion. `complete=False` means something was
    missed — the items returned are still perfectly good to create or
    update, but absences must be left alone.
    """

    items: list[FetchedItem] = field(default_factory=list)
    complete: bool = True
    # Why it was incomplete, in words a customer reading their own sync
    # status can act on ("the site was unreachable" is a different problem
    # from "your token expired"). Empty when complete.
    error: str = ""
