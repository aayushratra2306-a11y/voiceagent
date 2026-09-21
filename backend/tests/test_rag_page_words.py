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


# --- second review round: ambiguity must fail safe -----------------------------
#
# A WRONG page number is worse than none at all. The caller turns it into
# `{"page": {"$eq": n}}` on the vector search (rag.py), so a wrong number
# silently excludes the very page the caller asked for, and the bot answers
# from nothing. No number just means an unfiltered search, which still has a
# chance of finding the right passage. So anything ambiguous returns None.

def test_hundreds_spoken_with_and():
    """"one hundred and five" is how most of the world says 105 out loud."""
    assert _extract_page_num("page one hundred and five") == 105


def test_the_2026_09_14_garble_is_closed():
    """Logged live: "page twenty five" reached the server as "page twenty and"
    + "five", was rewritten to "page 20 and 5", and page 20 was answered."""
    assert _extract_page_num("page twenty and five") == 25


@pytest.mark.parametrize("query", [
    "page one thousand",      # not 1
    "page two thousand five",  # not 2
    "page one million",        # not 1
])
def test_numbers_we_cannot_parse_return_nothing_not_a_fragment(query):
    """Truncating "one thousand" to 1 would filter to the wrong page."""
    assert _extract_page_num(query) is None


@pytest.mark.parametrize("query", [
    "page one two three",    # digit-by-digit, or three separate pages?
    "page nineteen twenty",  # 1920, or page 19 then 20?
    "page ten hundred",      # not a number anyone says
    "page one hundred hundred",
])
def test_two_different_numbers_in_a_row_is_ambiguous(query):
    assert _extract_page_num(query) is None


@pytest.mark.parametrize("query,expected", [
    ("page eighty eighty", 80),
    ("page twenty twenty", 20),
    ("page five five", 5),
    ("page forty two forty two", 42),
])
def test_the_same_number_twice_is_the_caller_repeating_themselves(query, expected):
    assert _extract_page_num(query) == expected


def test_a_later_page_mention_is_found_without_punctuation_to_help():
    """The first "page" is innocent and nothing separates it from the real one."""
    query = "what is on the first page please turn to page eighty"
    assert _extract_page_num(query) == 80


def test_an_innocent_page_mention_before_a_real_one():
    assert _extract_page_num("about the page layout - and what is on page eighty?") == 80


# --- third review round: two different pages in one breath ---------------------
#
# Callers correct themselves out loud ("page one — no, page eighty") and the
# transcript runs it together with a comma or a "but", never a full stop.
# Reading only the first mention answers the page they just took back.
# Nothing distinguishes that from "compare page one and page eighty", where
# both are meant and neither alone is the answer, so the same rule applies as
# everywhere else here: two different readings means no filter.

@pytest.mark.parametrize("query", [
    "page one, page eighty",
    "start on page one but actually go to page eighty",
    "compare page one and page eighty",
    "page 1, page 80",
    "on page 50 there is a chart, what about page 51",
    "page eighty or was it page ninety",
])
def test_two_different_pages_named_means_no_filter(query):
    assert _extract_page_num(query) is None


@pytest.mark.parametrize("query,expected", [
    ("page eighty, page eighty", 80),
    ("page 80, page 80", 80),
    ("turn to page eighty and read me page eighty", 80),
])
def test_the_same_page_named_twice_still_answers(query, expected):
    assert _extract_page_num(query) == expected


def test_an_innocent_mention_does_not_count_as_a_second_page():
    """Only mentions that actually resolve to a number are compared."""
    assert _extract_page_num("the first page please turn to page eighty") == 80
    assert _extract_page_num("about the page layout, what is on page eighty?") == 80
