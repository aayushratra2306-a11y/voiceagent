"""Guards that a bot's language setting reaches everything that speaks.

Requested 2026-09-04: "language change karne pe yeah hindi mein greet kyun
nahi karta". The live log that day showed the setting reached STT (clean
Devanagari transcripts) and TTS (Cartesia spoke Hindi correctly) but never
the LLM, so a Hindi bot answered a Hindi question in English:

    user      नमस्कार जी, क्या आप मुझे sun पा रहे हैं?
    assistant Namaste! Yes, I can hear you loud and clear.

The greeting was the visible symptom; the model not knowing its own language
was the cause. Both are covered here, plus the "didn't catch that" fallback,
which has the same defect and is easy to forget because it only fires on a
failed turn.

The accent half of that request is deliberately NOT tested here — it depends
on which Cartesia voice a user picked for their bot, which is a product
decision about the voice catalogue rather than behaviour this module owns.
See language.py's docstring.
"""

import pytest

from app.pipeline.language import (
    GREETINGS,
    LANGUAGE_NAMES,
    didnt_catch_for,
    greeting_for,
    system_language_note,
)

# Exactly what the bot settings UI offers (BotSettingsPage.tsx LANGUAGES).
UI_LANGUAGES = ["en", "hi", "es", "fr", "de"]


@pytest.mark.parametrize("code", UI_LANGUAGES)
def test_every_language_the_ui_offers_has_its_own_spoken_text(code):
    """A language selectable in the UI but missing here silently falls back
    to English — the exact bug being fixed, reintroduced for a new language."""
    assert greeting_for(code) == GREETINGS[code]
    assert greeting_for(code) != GREETINGS["en"] or code == "en"
    assert didnt_catch_for(code)


def test_hindi_greeting_is_actually_devanagari_not_transliteration():
    """"Namaste" written in Latin letters would be read aloud by a TTS engine
    with English letter-to-sound rules. The script has to be real."""
    assert any("ऀ" <= ch <= "ॿ" for ch in greeting_for("hi"))


def test_the_model_is_told_which_language_to_speak():
    note = system_language_note("hi")
    assert "Hindi" in note, "the model is never told to reply in Hindi"


def test_mixed_language_input_does_not_flip_the_bot_to_english():
    """Deepgram transcribes real speech as spoken — 'क्या आप मुझे sun पा रहे
    हैं?' came back Hindi with an English word inside it. A borrowed English
    word is not a request to switch language, and the prompt has to say so."""
    note = system_language_note("hi").lower()
    assert "english" in note and "mix" in note


def test_english_adds_no_instruction_at_all():
    """English is the unprompted default, so the sentence would be prompt
    noise the model re-weighs on every turn."""
    assert system_language_note("en") == ""


@pytest.mark.parametrize("code", UI_LANGUAGES)
def test_language_note_names_the_language_for_every_non_english_option(code):
    note = system_language_note(code)
    if code == "en":
        assert note == ""
    else:
        assert LANGUAGE_NAMES[code] in note


@pytest.mark.parametrize("value", ["hi-IN", "HI", "fr-FR", "de-DE"])
def test_regional_and_uppercase_codes_normalise(value):
    """Matches providers._base_lang, so STT/TTS/prompt can never disagree
    about what language a call is in."""
    base = value.split("-")[0].lower()
    assert greeting_for(value) == GREETINGS[base]
    assert system_language_note(value) == system_language_note(base)


@pytest.mark.parametrize("value", [None, "", "zz", "klingon"])
def test_unknown_language_degrades_to_english_rather_than_breaking(value):
    """A worse greeting beats a crash or an empty one on a live call."""
    assert greeting_for(value) == GREETINGS["en"]
    assert didnt_catch_for(value)
    assert system_language_note(value) == ""


def test_pipeline_uses_the_helpers_rather_than_hardcoded_english():
    """Both call sites were hardcoded English strings; this fails if either
    is reintroduced."""
    import inspect

    from app.pipeline import voice_pipeline

    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "greeting_for(language)" in source, "greeting is hardcoded again"
    assert "didnt_catch_for(language)" in source, "fallback is hardcoded again"
    assert "system_language_note(language)" in source, (
        "the model is no longer told what language to reply in"
    )
    assert "Hello! I'm ready" not in source, "hardcoded English greeting is back"
