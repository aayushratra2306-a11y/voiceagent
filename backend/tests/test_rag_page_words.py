"""Live call 2026-09-21: the bot could not say what was on page 80 of its PDF.

The server log for that call shows why in two lines:

    [AUDIO] TranscriptionFrame: 'Check page eighty eighty.'
    [RAG] Query (unchanged): 'When why pay why not pay eighty? Check page
                              eighty eighty.'

"unchanged" is the whole story. _extract_page_num had been reduced to a
digit-only regex on the strength of a comment saying rewrite_query() turns
"page fifty" into "page 50" upstream, so the digit form was all it would
ever see. That call proves the assumption does not hold: the rewriter ran,
succeeded, and handed the spoken words straight back. No digits meant no
page number, which meant no page filter, which meant the search never
looked at page 80 at all — even though page 80 was indexed and had already
surfaced as a candidate on an earlier query in the same call.

The fix is not to make the rewriter more reliable. It is to stop depending
on a language model for something a parser can do exactly: read the number
here, and let the rewriter stay a bonus rather than a load-bearing step.

"eighty eighty" is the caller repeating themselves, which is what people do
on a bad line. Two tens words in a row cannot compose into one number, so
the second one starts a new number and the first is the answer — as opposed
to "twenty five", where a tens word followed by a unit word is one number.

No real Pinecone, OpenAI or database is touched in this file.
"""

import pytest

from app.services.rag import _extract_page_num

# --- the live failure ---------------------------------------------------------

def test_the_live_transcript_that_missed_page_80():
    """Verbatim from the 2026-09-21 call log. Returned None before the fix."""
    query = "When why pay why not pay eighty? Check page eighty eighty."
    assert _extract_page_num(query) == 80


def test_a_repeated_tens_word_is_a_repeat_not_a_sum():
    """"eighty eighty" is 80 said twice, never 160."""
    assert _extract_page_num("check page eighty eighty") == 80


# --- word forms ---------------------------------------------------------------

@pytest.mark.parametrize("query,expected", [
    ("what is on page eighty", 80),
    ("page fifty please", 50),
    ("read me page fifteen", 15),
    ("page seven", 7),
    ("page twelve", 12),
    ("go to page twenty five", 25),
    ("page forty two", 42),
    ("page ninety nine", 99),
    ("page one hundred", 100),
    ("page one hundred five", 105),
    ("page two hundred thirty one", 231),
])
def test_spoken_page_numbers_are_understood(query, expected):
    assert _extract_page_num(query) == expected


def test_case_does_not_matter():
    assert _extract_page_num("Page Eighty.") == 80


# --- the digit form must keep working -----------------------------------------

@pytest.mark.parametrize("query,expected", [
    ("what is on page 80", 80),
    ("page 50?", 50),
    ("PAGE 7", 7),
])
def test_digits_still_work(query, expected):
    assert _extract_page_num(query) == expected


def test_the_rewriter_doing_its_job_is_still_the_happy_path():
    """When rewrite_query does normalise the words, nothing changes."""
    assert _extract_page_num("check page 80") == 80


# --- when there is no page number ---------------------------------------------

@pytest.mark.parametrize("query", [
    "what does the document say about hooks",
    "",
    "tell me about the page layout",
    "page please",
    "what is on the first page",
])
def test_no_page_number_means_no_filter(query):
    assert _extract_page_num(query) is None


def test_a_number_word_that_is_not_about_a_page_is_ignored():
    """"pay eighty" in the live transcript must not be read as a page."""
    assert _extract_page_num("why not pay eighty") is None
