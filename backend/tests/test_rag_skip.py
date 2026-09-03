"""Latency (2026-09-03) — guards the conversational-turn skip.

A full retrieval cycle measures about 2.0s and used to run on every user
turn, including ones no document could possibly answer. needs_retrieval()
decides that, and it is the kind of word-list heuristic that quietly rots:
someone adds a word to make one utterance skip and silently breaks a real
question. These cases pin both directions.

The asymmetry matters. A missed skip costs 2 seconds. A WRONG skip means
the bot answers from general knowledge while the answer sits in the
customer's document — a silent quality regression nobody sees in a log.
So the false cases here are only ever pure filler, and anything carrying a
digit, a question mark, or a single real word must return True.
"""

import pytest

from app.pipeline.rag_processor import needs_retrieval

CONVERSATIONAL = [
    "hello", "hi there", "good morning", "thanks", "thank you",
    "ok thank you", "yeah okay sure", "sorry can you repeat that again",
    "bye", "one moment please", "",
]

NEEDS_LOOKUP = [
    "what is on page 20",          # digit
    "why?",                        # question mark
    "K?",                          # the real transcript that prompted this
    "what is that",                # question word, no filler-only match
    "how does it work",
    "tell me about MCP servers",
    "invoice status",
    "hello can you tell me what is on the page",  # greeting + real question
    "reset my password",
]


@pytest.mark.parametrize("text", CONVERSATIONAL)
def test_conversational_turns_skip_retrieval(text):
    assert needs_retrieval(text) is False, f"{text!r} should not trigger a lookup"


@pytest.mark.parametrize("text", NEEDS_LOOKUP)
def test_real_questions_still_retrieve(text):
    assert needs_retrieval(text) is True, (
        f"{text!r} must still retrieve — a wrong skip is a silent quality bug"
    )
