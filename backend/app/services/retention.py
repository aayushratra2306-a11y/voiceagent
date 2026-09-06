"""Task 6.3 — the other half of "let each customer turn it on or off":
automatically deleting a bot's transcripts once they pass its configured
retention period.

Per-bot, not a single global setting, because that is the manual's own
requirement — different customers have different retention commitments to
their own customers, and one platform-wide number cannot honour all of
them at once.

`recording_retention_days == 0` means "keep indefinitely" and is the
default (see models/bot.py) — a platform default that silently destroys a
customer's data because they never set a number would be its own kind of
incident, arguably worse than keeping it too long.

ConsentRecord is deliberately NEVER touched by this job. It is the proof
that a caller was told about recording; deleting it along with the
recording it was proof for defeats the entire reason it exists. A customer
who wants their consent logs gone too has that as a separate, explicit
action (task 6.5's export/deletion, when it exists) — not a side effect of
an unrelated retention number.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from loguru import logger

from app.models.bot import Bot
from app.models.conversation import ConversationTurn


async def purge_expired_transcripts() -> dict[str, int]:
    """One pass: for every bot with a real retention period configured,
    delete its transcripts older than that period.

    Returns bot_id -> rows deleted, for the caller (the loop below, and the
    tests) to see exactly what happened rather than trusting a log line.
    Never raises: one bot's bad data (a bot_id string that no longer
    resolves, an unexpected error from the driver) must not stop every
    OTHER bot's retention from being honoured — the same reasoning as
    TranscriptRecorder never letting a save failure interrupt a call.
    """
    deleted_by_bot: dict[str, int] = {}
    now = datetime.now(UTC)

    bots_with_retention = await Bot.find(
        Bot.recording_retention_days > 0
    ).to_list()

    for bot in bots_with_retention:
        bot_id = str(bot.id)
        cutoff = now - timedelta(days=bot.recording_retention_days)
        try:
            result = await ConversationTurn.find(
                ConversationTurn.bot_id == bot_id,
                ConversationTurn.created_at < cutoff,
            ).delete()
            # Motor's DeleteResult across Beanie's find().delete(): count is
            # on .deleted_count when a real result comes back, but some
            # driver/version combinations return None for a no-op delete —
            # treated as zero rather than let a None reach arithmetic or a
            # log line downstream.
            count = getattr(result, "deleted_count", 0) or 0
        except Exception as e:
            logger.warning(
                f"[RETENTION] Failed to purge transcripts for bot {bot_id}: {e}"
            )
            continue

        if count:
            logger.info(
                f"[RETENTION] Deleted {count} transcript(s) for bot {bot_id} "
                f"older than {bot.recording_retention_days}d"
            )
        deleted_by_bot[bot_id] = count

    return deleted_by_bot


async def retention_loop(interval_seconds: int = 3600) -> None:
    """Background loop (started from main.py's lifespan).

    Hourly rather than more often: retention is measured in DAYS, so
    running this every few seconds like the webhook outbox (task 3.8, a
    genuinely time-sensitive queue) buys nothing — it would only mean more
    chances for one run's failure-logging to spam the log for no benefit.
    A transcript living a few minutes past its exact expiry, in a job that
    runs once an hour, is not the failure mode this task is guarding
    against.
    """
    while True:
        try:
            await purge_expired_transcripts()
        except Exception as e:
            # Belt and braces on top of purge_expired_transcripts' own
            # per-bot try/except: a bug in the loop itself (not in any one
            # bot's deletion) must not silently end retention enforcement
            # for the life of the process.
            logger.error(f"[RETENTION] purge pass failed: {e}")
        await asyncio.sleep(interval_seconds)
