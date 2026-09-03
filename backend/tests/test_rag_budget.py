"""Guards the retrieval budget (found live 2026-09-03).

RAGContextProcessor sits BEFORE the user aggregator and does not push the
transcript frame until retrieval finishes. The aggregator has its own
`user_turn_stop_timeout` (5.0s in pipecat), after which it concludes no
transcript is coming and makes the bot say "Sorry, I didn't catch that".

So a slow lookup produced exactly the wrong behaviour: the bot had heard the
caller perfectly, was still searching, and apologised for mishearing while
discarding the turn. Measured live: retrieval 7.03s, aggregator gave up at
4.1s.

The budget must therefore stay meaningfully BELOW the aggregator's timeout.
These tests pin that relationship, because the failure it prevents is
invisible in code review — the two numbers live in different libraries.
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
