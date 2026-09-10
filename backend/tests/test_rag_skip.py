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

Hindi cases added 2026-09-10 (review finding): the word match was
`[a-z']+`, so a pure-Devanagari turn produced no words and skipped
retrieval entirely — and Deepgram rarely emits `?` for Devanagari, so a
Hindi document question hit none of the guards. Every case below is a real
question that must retrieve. A Hindi greeting is deliberately NOT tested as
a skip: the filler list is romanized, so "नमस्ते" retrieves (a wasted
lookup), which is the accepted direction to be wrong.
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


# Real Hindi questions — no ASCII letters, and (as Deepgram transcribes
# them) usually no '?'. Every one skipped retrieval entirely before the fix.
HINDI_NEEDS_LOOKUP = [
    "दस्तावेज़ में वारंटी की जानकारी क्या है",   # "what's the warranty info in the document"
    "मेरा ऑर्डर कहाँ है",                         # "where is my order"
    "गारंटी कितने साल की है।",                    # "how many years is the guarantee" — ends with a danda, not ?
    "पेज ५० पर क्या लिखा है",                     # "what's written on page 50" — Devanagari digits
    "रिफंड की पॉलिसी बताओ",                       # "tell me the refund policy"
]


@pytest.mark.parametrize("text", CONVERSATIONAL)
def test_conversational_turns_skip_retrieval(text):
    assert needs_retrieval(text) is False, f"{text!r} should not trigger a lookup"


@pytest.mark.parametrize("text", NEEDS_LOOKUP)
def test_real_questions_still_retrieve(text):
    assert needs_retrieval(text) is True, (
        f"{text!r} must still retrieve — a wrong skip is a silent quality bug"
    )


@pytest.mark.parametrize("text", HINDI_NEEDS_LOOKUP)
def test_hindi_questions_still_retrieve(text):
    assert needs_retrieval(text) is True, (
        f"{text!r} must retrieve — before the fix a pure-Devanagari turn "
        f"produced no words and skipped RAG, answering from general knowledge"
    )
