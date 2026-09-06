"""Task 6.2 — take sensitive values out of a transcript before it is stored.

The manual's framing is liability, and it is right: people read card numbers
out loud constantly, and Phase 3's payment rule (tool_registry.py's
PAYMENT_SAFETY_RULE) tells the BOT never to ask for one — it cannot stop a
caller volunteering it anyway. This is the layer that assumes they did.

The tip that shapes every decision here: **redact before it touches disk,
not afterwards.** Once a card number has been written to the database, a log
file or a backup, cleaning it up properly is genuinely hard and you are
never quite sure you got all of it. So this runs in
TranscriptRecorder._finalize_turn, between building the record and
inserting it, and there is deliberately no "redact later" path anywhere.

Two decisions worth explaining, because both look wrong at first glance.

**Any long run of digits goes, whether or not it looks like a real card.**
A 16-digit run that fails its Luhn checksum is still 16 digits nobody needs
in a transcript, and it is at least as likely to be a real card that speech
recognition got one digit wrong as it is to be something innocent. The
marker says which case it was — `[card number]` when the checksum passes,
`[long number]` when it does not — so the distinction survives for anyone
reading, without either kind being stored. Getting the LABEL wrong is
cosmetic; storing the DIGITS is the actual liability.

**Spoken digits are handled as well as written ones.** Deepgram usually
formats numbers as digits, but "usually" is not a property to build a
compliance control on, and a caller reading a card aloud slowly is exactly
when it degrades. A run of twelve or more number-words gets the same
treatment as a run of twelve or more digits.

Ordering matters and is deliberate: the most specific patterns run first, so
a +91 mobile number is recognised as a phone rather than caught by the
twelve-digit identity-number rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The kinds this module can find. Exposed as a set so a bot can narrow it —
# a healthcare customer may want addresses gone that a pizza shop does not
# care about, and the manual asks for it to be configurable per customer.
ALL_KINDS = frozenset({
    "card", "cvv", "aadhaar", "pan", "phone", "email", "spoken_digits",
})

# Deliberately every kind. An operator narrowing this is making an informed
# choice; an operator who has not thought about it should get the safe
# default rather than silent storage of card numbers.
DEFAULT_KINDS = ALL_KINDS


@dataclass(frozen=True)
class RedactionResult:
    text: str
    # Which kinds were actually found. Recorded on the turn so an operator
    # can see that redaction happened without the transcript having to show
    # what was taken out.
    kinds: tuple[str, ...]


# Digit-word runs. "four one one one ..." — a caller reading a card aloud.
_NUMBER_WORDS = {
    "zero", "oh", "one", "two", "three", "four", "five", "six", "seven",
    "eight", "nine", "double", "triple",
    # Hindi, because half this project's callers speak it and a compliance
    # control that only works in English is not a compliance control. These
    # are the Latin-script spellings Deepgram's Hindi model actually emits.
    "shunya", "ek", "do", "teen", "char", "chaar", "paanch", "panch",
    "cheh", "chhe", "saat", "aath", "nau",
}

_SEP = r"[\s\-.]"  # what people put between groups of digits


def _digits_only(text: str) -> str:
    return re.sub(r"\D", "", text)


def _luhn_ok(number: str) -> bool:
    """The checksum every real card number satisfies. Used ONLY to choose
    the marker's wording — never to decide whether to redact."""
    if not number.isdigit():
        return False
    total, parity = 0, len(number) % 2
    for i, ch in enumerate(number):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _redact_card_like(text: str, found: set[str]) -> str:
    """13-19 digits, in any grouping people actually speak or type."""

    def _replace(match: re.Match) -> str:
        digits = _digits_only(match.group(0))
        if not 13 <= len(digits) <= 19:
            return match.group(0)
        found.add("card")
        return "[card number]" if _luhn_ok(digits) else "[long number]"

    return re.sub(rf"\b\d(?:{_SEP}?\d){{12,18}}\b", _replace, text)


def _redact_aadhaar(text: str, found: set[str]) -> str:
    """Exactly 12 digits — India's national identity number, normally
    spoken and written in three groups of four."""

    def _replace(match: re.Match) -> str:
        if len(_digits_only(match.group(0))) != 12:
            return match.group(0)
        found.add("aadhaar")
        return "[id number]"

    return re.sub(rf"\b\d(?:{_SEP}?\d){{11}}\b", _replace, text)


def _redact_pan(text: str, found: set[str]) -> str:
    """India's tax identity number: five letters, four digits, one letter.
    Distinctive enough that a false positive is very unlikely."""

    def _replace(match: re.Match) -> str:
        found.add("pan")
        return "[tax id]"

    return re.sub(r"\b[A-Z]{5}\d{4}[A-Z]\b", _replace, text, flags=re.IGNORECASE)


def _redact_phone(text: str, found: set[str]) -> str:
    """Indian mobile numbers, with or without the country code. Run BEFORE
    the twelve-digit identity rule, or '+91 98765 43210' — twelve digits —
    would be filed as an Aadhaar number."""

    def _replace(match: re.Match) -> str:
        found.add("phone")
        return "[phone number]"

    return re.sub(
        rf"(?:\+?91{_SEP}?)?\b[6-9]\d(?:{_SEP}?\d){{8}}\b", _replace, text
    )


def _redact_email(text: str, found: set[str]) -> str:
    def _replace(match: re.Match) -> str:
        found.add("email")
        return "[email address]"

    return re.sub(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", _replace, text)


def _redact_cvv(text: str, found: set[str]) -> str:
    """Three or four digits are far too common to redact on sight — that
    would eat every price, quantity and year in every transcript. Only
    redacted when the caller has said what the number IS."""

    def _replace(match: re.Match) -> str:
        found.add("cvv")
        return f"{match.group(1)}{match.group(2)}[security code]"

    return re.sub(
        r"\b(cvv|cvc|security\s+code|card\s+verification(?:\s+code)?)\b"
        r"(\s*(?:is|:)?\s*)\d{3,4}\b",
        _replace,
        text,
        flags=re.IGNORECASE,
    )


def _redact_spoken_digits(text: str, found: set[str]) -> str:
    """A run of twelve or more number-words: someone reading a long number
    out loud, in a transcript speech recognition did not format as digits.

    Twelve rather than sixteen because a caller saying "double four" for
    "44" produces fewer words than digits, and because twelve is already
    far longer than any ordinary sentence's run of numbers.
    """
    words = re.findall(r"\S+", text)
    if len(words) < 12:
        return text

    def _is_number_word(word: str) -> bool:
        return re.sub(r"[^\w]", "", word).lower() in _NUMBER_WORDS

    out: list[str] = []
    i = 0
    while i < len(words):
        j = i
        while j < len(words) and _is_number_word(words[j]):
            j += 1
        if j - i >= 12:
            found.add("spoken_digits")
            out.append("[long number]")
        else:
            out.extend(words[i:j])
        if j == i:
            out.append(words[i])
            i += 1
        else:
            i = j
    return " ".join(out)


# Order is load-bearing: most specific first, so a phone number is a phone
# number rather than a twelve-digit identity number, and a card number is
# gone before the shorter rules get a chance to mangle part of it.
_RULES = (
    ("email", _redact_email),
    ("pan", _redact_pan),
    ("cvv", _redact_cvv),
    ("card", _redact_card_like),
    ("phone", _redact_phone),
    ("aadhaar", _redact_aadhaar),
    ("spoken_digits", _redact_spoken_digits),
)


def redact(text: str, kinds: frozenset[str] | set[str] | None = None) -> RedactionResult:
    """Take the sensitive values out of one piece of text.

    Never raises. A redaction bug must not be able to stop a transcript
    being written at all — losing the conversation record to protect it is
    not a trade worth making, and the caller is already off the call by the
    time this runs.
    """
    if not text:
        return RedactionResult(text=text, kinds=())

    enabled = ALL_KINDS if kinds is None else (set(kinds) & ALL_KINDS)
    found: set[str] = set()
    out = text
    try:
        for kind, rule in _RULES:
            if kind in enabled:
                out = rule(out, found)
    except Exception:  # pragma: no cover - defensive, see the docstring
        # Fall back to redacting nothing rather than storing a half-processed
        # string, and let the caller decide. Returning the ORIGINAL is the
        # safe answer for correctness but the unsafe one for privacy, which
        # is why the loop above is built out of independent, total rules
        # rather than one clever pass that can half-fail.
        return RedactionResult(text=out, kinds=tuple(sorted(found)))
    return RedactionResult(text=out, kinds=tuple(sorted(found)))


def redact_structure(value, kinds: frozenset[str] | set[str] | None = None):
    """Walk a tool call's arguments or result and redact every string in it.

    Tool payloads are the other way a card number reaches the database: a
    customer's own "take a payment" tool can carry one in its arguments, and
    that record is stored on the turn exactly like the transcript is.
    Returns (value, kinds_found).
    """
    found: set[str] = set()

    def _walk(node):
        if isinstance(node, str):
            result = redact(node, kinds)
            found.update(result.kinds)
            return result.text
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [_walk(v) for v in node]
        return node

    return _walk(value), tuple(sorted(found))
