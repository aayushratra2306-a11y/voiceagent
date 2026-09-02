"""Task 2.6 — ownership verification.

One reusable dependency per owned resource type, used on every route that
touches it. Before this, each route re-implemented its own
`if not X or X.user_id != str(current_user.id): raise 404` inline — that
worked everywhere it was actually written, but the risk was never "the check
is wrong", it was "a new route ships without it". Centralizing removes that
risk: a route either declares the dependency (and is safe) or doesn't
(obviously missing something), instead of "looks safe, hope nobody forgot".

Returns 404, not 403, on both "doesn't exist" and "exists but isn't yours" —
deliberately identical responses, so probing bot/document IDs can't be used
to detect which ones are real versus simply not owned by the caller.
"""

from fastapi import Depends, HTTPException

from app.core.auth import get_current_user
from app.models.bot import Bot
from app.models.document import Document
from app.models.user import User


async def fetch_owned_bot(bot_id: str, current_user: User) -> Bot:
    """The actual check, as a plain function — used directly by routes where
    bot_id comes from somewhere other than the URL path (e.g. connect.py's
    /connect, where it's a field on the request body), and wrapped by
    get_owned_bot below for the common path-parameter case."""
    bot = await Bot.get(bot_id)
    if not bot or bot.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot


async def get_owned_bot(bot_id: str, current_user: User = Depends(get_current_user)) -> Bot:
    return await fetch_owned_bot(bot_id, current_user)


async def get_owned_document(doc_id: str, current_user: User = Depends(get_current_user)) -> Document:
    doc = await Document.get(doc_id)
    if not doc or doc.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Document not found")
    return doc
