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
    assert greeting_for(code) in GREETINGS[code].values()
    assert greeting_for(code) != GREETINGS["en"]["female"] or code == "en"
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
    assert greeting_for(value) == greeting_for(base)
    assert system_language_note(value) == system_language_note(base)


@pytest.mark.parametrize("value", [None, "", "zz", "klingon"])
def test_unknown_language_degrades_to_english_rather_than_breaking(value):
    """A worse greeting beats a crash or an empty one on a live call."""
    assert greeting_for(value) == GREETINGS["en"]["female"]
    assert didnt_catch_for(value)
    assert system_language_note(value) == ""


def test_pipeline_uses_the_helpers_rather_than_hardcoded_english():
    """Both call sites were hardcoded English strings; this fails if either
    is reintroduced."""
    import inspect

    from app.pipeline import voice_pipeline

    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "greeting_for(language" in source, "greeting is hardcoded again"
    assert "didnt_catch_for(language" in source, "fallback is hardcoded again"
    assert "system_language_note(language" in source, (
        "the model is no longer told what language to reply in"
    )
    assert "Hello! I'm ready" not in source, "hardcoded English greeting is back"


# --- Voice selection (added 2026-09-04) --------------------------------------
#
# The settings UI offered three voices, all English, to every bot whatever
# its language. A Hindi bot therefore HAD to be given an English voice, and
# Cartesia — multilingual — read Hindi words with English mouth-shapes. That
# is the accent the user asked about, and it is stored in every bot already
# in the database, so correcting the dropdown alone would leave them wrong.

from app.pipeline.language import VOICES, default_voice_for, resolve_voice  # noqa: E402

HINDI_SNEHA = "6b02ffe5-e3cb-48c0-a023-c72f85953375"
HINDI_VIKAS = "adf97b9d-905c-41de-9fe9-afb387116d06"
ENGLISH_GREG = "a0e99841-438c-4a64-b679-ae501e7d6091"


def test_an_english_voice_on_a_hindi_bot_is_corrected():
    """The exact state of every bot saved before per-language voices existed."""
    assert resolve_voice(ENGLISH_GREG, "hi") == HINDI_SNEHA


@pytest.mark.parametrize("code", UI_LANGUAGES)
def test_every_ui_language_has_native_voices(code):
    assert VOICES.get(code), f"{code} is selectable but has no voice of its own"
    assert default_voice_for(code) == VOICES[code][0]["id"]


@pytest.mark.parametrize("voice", [HINDI_SNEHA, HINDI_VIKAS])
def test_a_voice_already_right_for_the_language_is_left_alone(voice):
    """Both Hindi voices are valid choices — picking the male one must not be
    silently reset to the female default on every call."""
    assert resolve_voice(voice, "hi") == voice


def test_an_unknown_voice_is_never_second_guessed():
    """A custom or cloned voice has no language recorded here. Overriding it
    with a default would break a deliberate choice — worse than the bug."""
    custom = "00000000-1111-2222-3333-444444444444"
    assert resolve_voice(custom, "hi") == custom


def test_a_bot_with_no_voice_at_all_still_gets_one():
    assert resolve_voice(None, "hi") == HINDI_SNEHA
    assert resolve_voice("", "fr") == default_voice_for("fr")


def test_regional_language_codes_resolve_voices_too():
    assert resolve_voice(ENGLISH_GREG, "hi-IN") == HINDI_SNEHA


def test_unknown_language_falls_back_to_english_voices():
    assert default_voice_for("klingon") == VOICES["en"][0]["id"]


def test_no_voice_id_is_registered_under_two_languages():
    """A duplicated id would make _VOICE_LANGUAGE ambiguous and could bounce
    a voice between languages depending on dict order."""
    ids = [v["id"] for vs in VOICES.values() for v in vs]
    assert len(ids) == len(set(ids)), "a voice id appears under two languages"


def test_backend_and_frontend_voice_catalogues_agree():
    """Two hardcoded lists that must not drift: the UI offers these, and the
    server corrects to them. A voice in one but not the other means a user
    picks something the server then silently overrides."""
    import re
    from pathlib import Path

    tsx = Path(__file__).resolve().parents[2] / "frontend/src/pages/BotSettingsPage.tsx"
    source = tsx.read_text(encoding="utf-8")
    block = source[source.index("const VOICES"):source.index("const voicesFor")]
    frontend_ids = set(re.findall(r"id: '([0-9a-f-]{36})'", block))
    backend_ids = {v["id"] for vs in VOICES.values() for v in vs}

    assert frontend_ids == backend_ids, (
        f"only in UI: {frontend_ids - backend_ids}; "
        f"only in backend: {backend_ids - frontend_ids}"
    )


def test_tts_factory_actually_applies_the_correction():
    """End of the chain: a Hindi bot holding an English voice must reach
    Cartesia with the Hindi one, not merely be corrected in a helper."""
    from app.pipeline.providers import get_tts_service

    svc = get_tts_service(voice_id=ENGLISH_GREG, language="hi")
    assert svc._settings.voice == HINDI_SNEHA, (
        "the English voice reached Cartesia — Hindi would be spoken with an "
        "English accent, which is the bug this exists to prevent"
    )


def test_the_language_itself_reaches_cartesia():
    """The FOURTH instance of this project's silent-dropped-kwarg pattern.
    `language=` as a bare kwarg to CartesiaTTSService is discarded and left at
    'en', so every Hindi call was synthesised with Cartesia told the text was
    English — an accent problem entirely separate from which voice is picked,
    and one no log line or error would ever reveal.
    """
    from app.pipeline.providers import get_tts_service

    svc = get_tts_service(voice_id=HINDI_SNEHA, language="hi")
    assert svc._settings.language == "hi", (
        "Cartesia was told the text is English while being handed Hindi — "
        "even a native Hindi voice is then pronounced with English rules"
    )


def test_regional_language_codes_reach_cartesia_normalised():
    """Cartesia wants 'hi', not 'hi-IN'."""
    from app.pipeline.providers import get_tts_service

    assert get_tts_service(voice_id=HINDI_SNEHA, language="hi-IN")._settings.language == "hi"


# --- Speaker gender agreement (found by the user on a live call 2026-09-04) --
#
# The Hindi bot speaks with Sneha, a female voice, and said "मैं आपकी क्या मदद
# कर सकता हूँ?" — the MASCULINE form. Hindi verbs agree with the speaker's own
# gender, so a female voice must say "कर सकती हूँ". Not a nitpick: it is
# audibly wrong to any Hindi speaker. French and Spanish inflect the same way
# and their defaults (Audrey, Marta) are female too, so both were wrong
# identically.

from app.pipeline.language import GENDERED_SELF_REFERENCE, voice_gender  # noqa: E402


def test_hindi_female_voice_uses_feminine_verb_forms():
    """The exact sentence the user heard and corrected."""
    assert "सकती" in greeting_for("hi", "female")
    assert "सकता" not in greeting_for("hi", "female")


def test_hindi_male_voice_still_uses_masculine_forms():
    assert "सकता" in greeting_for("hi", "male")
    assert "सकती" not in greeting_for("hi", "male")


def test_hindi_fallback_agrees_with_gender_too():
    """Easy to miss — it only fires on a failed turn."""
    assert "पाई" in didnt_catch_for("hi", "female")
    assert "पाया" in didnt_catch_for("hi", "male")


@pytest.mark.parametrize(
    "code,feminine,masculine",
    [("fr", "prête", "prêt"), ("es", "lista", "listo")],
)
def test_the_same_agreement_applies_in_french_and_spanish(code, feminine, masculine):
    """Both default voices there are female, so both had the identical bug."""
    assert feminine in greeting_for(code, "female")
    assert masculine in greeting_for(code, "male")
    assert greeting_for(code, "female") != greeting_for(code, "male")


def test_french_apology_agrees_as_well():
    assert didnt_catch_for("fr", "female").startswith("Désolée")
    assert didnt_catch_for("fr", "male").startswith("Désolé,")


@pytest.mark.parametrize("code", ["en", "de"])
def test_languages_without_this_agreement_say_the_same_thing_either_way(code):
    """English and German don't inflect these; differing forms would mean a
    typo crept into one half."""
    assert greeting_for(code, "female") == greeting_for(code, "male")
    assert didnt_catch_for(code, "female") == didnt_catch_for(code, "male")


def test_the_model_is_told_the_voices_gender():
    """The two fixed strings are a fraction of what the caller hears — the
    model writes the rest, and on the live call it said "सुन रहा हूँ"
    (masculine) through a female voice."""
    note = system_language_note("hi", "female")
    assert "feminine" in note
    assert "masculine" in system_language_note("hi", "male")


@pytest.mark.parametrize("code", sorted(GENDERED_SELF_REFERENCE))
def test_every_gendered_language_tells_the_model(code):
    assert "feminine" in system_language_note(code, "female")


@pytest.mark.parametrize("code", ["en", "de"])
def test_ungendered_languages_do_not_waste_prompt_on_it(code):
    assert "feminine" not in system_language_note(code, "female")


def test_gender_follows_the_actual_voice_not_a_guess():
    assert voice_gender(HINDI_SNEHA) == "female"
    assert voice_gender(HINDI_VIKAS) == "male"


def test_unknown_voice_falls_back_to_the_languages_default_gender():
    """Must not return None — there is no ungendered way to say "I can help
    you" in Hindi, so a None would only move the same guess further up."""
    assert voice_gender("not-a-real-voice", "hi") == "female"  # Sneha is the default


def test_wording_matches_the_voice_end_to_end():
    """The whole point: pick the male Hindi voice and the words change with
    it, without anyone passing gender by hand."""
    resolved = resolve_voice(HINDI_VIKAS, "hi")
    assert "सकता" in greeting_for("hi", voice_gender(resolved, "hi"))


def test_pipeline_passes_the_voices_gender_through():
    import inspect

    from app.pipeline import voice_pipeline

    source = inspect.getsource(voice_pipeline.run_voice_pipeline)
    assert "voice_gender(voice_id, language)" in source
    for call in ("greeting_for(language, speaking_gender)",
                 "didnt_catch_for(language, speaking_gender)",
                 "system_language_note(language, speaking_gender)"):
        assert call in source, f"{call} is not wired through"
