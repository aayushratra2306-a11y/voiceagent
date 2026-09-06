"""Task 6.2 — sensitive values never reach the transcript.

The manual's own tip drives the shape of these tests: "redact before it
touches disk... you will never be completely sure you got it all." So the
bar here is not "the happy-path example works" — it is spoken digits,
grouped digits, a card number with one digit speech recognition got wrong,
and Hindi number words, because those are the actual ways a real call
produces this text.
"""

from app.core.redaction import ALL_KINDS, redact, redact_structure


def test_a_written_card_number_is_replaced():
    result = redact("my card number is 4111 1111 1111 1111 thanks")
    assert "4111" not in result.text
    assert "[card number]" in result.text
    assert result.kinds == ("card",)


def test_a_card_number_with_no_separators_is_still_caught():
    result = redact("4111111111111111")
    assert "4111" not in result.text
    assert result.kinds == ("card",)


def test_a_card_number_with_dashes_is_still_caught():
    result = redact("4111-1111-1111-1111")
    assert "4111" not in result.text


def test_a_failing_checksum_is_still_redacted_but_labelled_differently():
    """A card number with one digit wrong (speech recognition mishearing a
    word) still has to go — the digits are the liability, not whether they
    happen to be a technically valid card."""
    # 4111111111111112 fails Luhn (the real number ends in 1).
    result = redact("4111 1111 1111 1112")
    assert "1112" not in result.text
    assert "[long number]" in result.text
    assert result.kinds == ("card",)


def test_a_cvv_is_only_redacted_when_named_as_one():
    """Three or four digits are far too common on their own — a price, a
    quantity, a year. Redacting on sight would eat all of those."""
    named = redact("the cvv is 482")
    assert "482" not in named.text
    assert named.kinds == ("cvv",)

    unnamed = redact("that will be 482 rupees")
    assert "482" in unnamed.text
    assert unnamed.kinds == ()


def test_an_aadhaar_number_is_replaced():
    result = redact("my aadhaar is 234512345678")
    assert "234512345678" not in result.text
    assert "[id number]" in result.text
    assert result.kinds == ("aadhaar",)


def test_a_pan_number_is_replaced():
    result = redact("PAN is ABCDE1234F")
    assert "ABCDE1234F" not in result.text
    assert result.kinds == ("pan",)


def test_an_indian_mobile_number_is_recognised_as_a_phone_not_an_identity_number():
    """The ordering rule this whole module depends on: a twelve-digit
    +91 mobile must not be caught by the twelve-digit Aadhaar rule."""
    result = redact("call me on +91 98765 43210")
    assert "98765" not in result.text
    assert result.kinds == ("phone",)
    assert "id number" not in result.text


def test_a_mobile_number_without_the_country_code_is_still_caught():
    result = redact("my number is 9876543210")
    assert "9876543210" not in result.text
    assert result.kinds == ("phone",)


def test_an_email_address_is_replaced():
    result = redact("send it to priya.sharma@example.com please")
    assert "priya.sharma" not in result.text
    assert result.kinds == ("email",)


def test_spoken_digits_are_caught_even_when_never_written_as_numbers():
    """Deepgram usually formats numbers as digits, but a caller reading a
    card slowly aloud is exactly the case where that degrades — this must
    not depend on STT getting the formatting right."""
    result = redact(
        "the number is four one one one one one one one one one one one one one one one one"
    )
    assert "[long number]" in result.text
    assert "spoken_digits" in result.kinds


def test_spoken_digits_in_hindi_are_also_caught():
    """Half this project's callers speak Hindi (see language.py), and a
    compliance control that only works in English is not one."""
    result = redact("ek do teen char paanch cheh saat aath nau ek do teen char")
    assert "[long number]" in result.text


def test_a_short_run_of_number_words_is_left_alone():
    """The threshold exists so ordinary sentences survive. 'I have two
    kids and one dog' must not be mangled."""
    result = redact("I have two kids and one dog, see you at five")
    assert result.text == "I have two kids and one dog, see you at five"
    assert result.kinds == ()


def test_ordinary_conversation_is_completely_untouched():
    text = "Hi, I'd like to check on order 4471 please, it was placed last Tuesday"
    result = redact(text)
    # Nothing sensitive here by design — order numbers are not on the
    # redacted list, and nothing should have been altered.
    assert result.text == text


def test_multiple_kinds_in_one_utterance_are_all_caught():
    result = redact(
        "my card is 4111 1111 1111 1111, cvv is 482, and my number is 9876543210"
    )
    assert "4111" not in result.text
    assert "482" not in result.text
    assert "9876543210" not in result.text
    assert set(result.kinds) == {"card", "cvv", "phone"}


def test_narrowing_the_enabled_kinds_leaves_the_others_alone():
    """The manual asks for this to be configurable per customer — a
    healthcare bot may want addresses redacted that a pizza shop does not
    care about."""
    result = redact("my card is 4111 1111 1111 1111", kinds={"phone"})
    assert "4111" in result.text
    assert result.kinds == ()


def test_never_raises_on_adversarial_input():
    """A redaction bug must not be able to take the transcript-saving path
    down with it — see redact()'s own docstring."""
    for bad in ("", None, "🎉" * 500, "\x00\x01\x02", "a" * 100_000, "4" * 50):
        redact(bad or "")  # must not raise


def test_redact_structure_walks_a_tool_calls_arguments():
    """Tool payloads are the other way a card number reaches the database —
    a payment tool's own arguments, stored on the turn record."""
    payload = {
        "card_number": "4111111111111111",
        "amount": 500,
        "notes": ["cvv is 482", "no sensitive data here"],
    }
    redacted, kinds = redact_structure(payload)

    assert "4111" not in str(redacted)
    assert "482" not in str(redacted)
    assert redacted["amount"] == 500, "a non-string value was altered"
    assert redacted["notes"][1] == "no sensitive data here"
    assert set(kinds) == {"card", "cvv"}


def test_redact_structure_handles_nested_lists_and_dicts():
    payload = {"customer": {"contacts": [{"email": "a@b.com"}, {"phone": "9876543210"}]}}
    redacted, kinds = redact_structure(payload)

    assert "a@b.com" not in str(redacted)
    assert "9876543210" not in str(redacted)
    assert set(kinds) == {"email", "phone"}


def test_all_kinds_is_the_default():
    result = redact("test")
    assert result.kinds == ()
    # Confirms nothing is silently narrowed by default — every kind this
    # module knows about is active unless a caller explicitly narrows it.
    from app.core import redaction

    assert redaction.DEFAULT_KINDS == ALL_KINDS
