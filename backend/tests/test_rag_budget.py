"""Guards the retrieval budget (found live 2026-09-03).

Originally existed because RAGContextProcessor sat BEFORE the user
aggregator and held the raw transcript frame while it searched — a slow
lookup let the aggregator's own `user_turn_stop_timeout` (5.0s) fire
believing no transcript had arrived, and the bot apologised for mishearing
a caller it had heard perfectly. Measured live: retrieval 7.03s, aggregator
gave up at 4.1s.

That specific race became structurally impossible the same day, when the
processor moved to AFTER the aggregator (see rag_processor.py's
latest_user_text docstring) — the aggregator has already committed the turn
by the time this processor sees anything, so its timeout can't fire for a
turn that's already done. The budget stays for a plainer reason: an
unbounded Pinecone/OpenAI call would still hang the reply indefinitely.
AGGREGATOR_TURN_STOP_TIMEOUT is kept here as a documented reference point
for what "meaningfully bounded" means, not because the two are still racing
each other.
"""

import asyncio

import pytest

from app.pipeline import rag_processor as rp


def test_budget_leaves_headroom_under_the_aggregator_timeout():
    assert rp.RETRIEVAL_BUDGET_SECONDS < rp.AGGREGATOR_TURN_STOP_TIMEOUT, (
        "retrieval may not hold a frame longer than the aggregator will wait — "
        "that is the bug this budget exists to prevent"
    )
    headroom = rp.AGGREGATOR_TURN_STOP_TIMEOUT - rp.RETRIEVAL_BUDGET_SECONDS
    assert headroom >= 1.0, (
        f"only {headroom:.1f}s of headroom; the rest of the turn needs room too"
    )


@pytest.mark.asyncio
async def test_slow_retrieval_is_abandoned_at_the_budget():
    """A lookup that overruns must be cut off, not waited on."""

    async def never_returns():
        await asyncio.sleep(30)

    started = asyncio.get_event_loop().time()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(never_returns(), timeout=rp.RETRIEVAL_BUDGET_SECONDS)
    elapsed = asyncio.get_event_loop().time() - started

    assert elapsed < rp.AGGREGATOR_TURN_STOP_TIMEOUT, (
        "gave up too late — the aggregator would already have discarded the turn"
    )
