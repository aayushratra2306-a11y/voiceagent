"""Per-language spoken text for the bits of the call the LLM doesn't write.

Requested 2026-09-04 ("language change karne pe yeah hindi mein greet kyun
nahi karta"), and the live log from that same day showed the problem was
wider than the greeting. A bot with language='hi' had its setting correctly
reaching STT (Deepgram transcribed clean Devanagari) and TTS (Cartesia spoke
Hindi fine) — but nothing ever told the LLM. Its entire system prompt was
English, so it answered a Hindi question in English:

    user      नमस्कार जी, क्या आप मुझे sun पा रहे हैं?
    assistant Namaste! Yes, I can hear you loud and clear.

The caller then had to ask "क्या हम हिंदी में बातचीत कर सकते हैं?" before it
switched. So the bot's language setting drove how it HEARD and how it
SOUNDED, but not what language it actually thought in — the greeting was
just the first and most visible symptom of that.

Three things need the language, and they are genuinely different problems:

  1. The greeting and the "didn't catch that" fallback. Fixed here — these
     are fixed strings the LLM never sees, so they have to be written out
     per language rather than generated.
  2. What the model replies in. Fixed here too, via system_language_note().
  3. The ACCENT. Not solved here and not solvable here: the voice is the
     bot's own user-chosen Cartesia voice_id. Cartesia Sonic is multilingual
     so it will speak the words, but an English-native voice speaking Hindi
     carries an English accent. Making accent follow language needs a
     per-language voice catalogue in the bot-creation UI, which is a product
     decision about which voices to offer, not something to guess at here.

Keys are base language codes ('hi', not 'hi-IN') to match _base_lang() in
providers.py — the same normalisation the STT/TTS factories already use.
Covers exactly the five languages the bot settings UI offers today; anything
else falls back to English, which is a worse greeting but never a broken
one.
"""

# Caught by the user on a live call 2026-09-04: the Hindi bot speaks with
# Sneha, a female voice, and said "मैं आपकी क्या मदद कर सकता हूँ?" — the
# MASCULINE form. In Hindi the verb agrees with the speaker's own gender, so
# a female voice has to say "कर सकती हूँ". Saying it the other way is not a
# stylistic nitpick; it is audibly wrong to any Hindi speaker.
#
# The same agreement exists in French ("je suis prêt" / "prête", "désolé" /
# "désolée") and Spanish ("estoy listo" / "lista"), and our default voices
# there — Audrey and Marta — are female too, so both were wrong in exactly
# the same way. German and English don't inflect these, hence _same().
#
# _same() rather than repeating a string twice: it states outright that the
# language has no distinction here, and makes the two halves impossible to
# edit out of sync later.
def _same(text: str) -> dict[str, str]:
    """For languages where the phrasing doesn't change with speaker gender."""
    return {"female": text, "male": text}


GREETINGS = {
    "en": _same("Hello! I'm ready. How can I help you?"),
    "hi": {
        "female": "नमस्ते! मैं तैयार हूँ। मैं आपकी क्या मदद कर सकती हूँ?",
        "male": "नमस्ते! मैं तैयार हूँ। मैं आपकी क्या मदद कर सकता हूँ?",
    },
    "es": {
        "female": "¡Hola! Estoy lista. ¿En qué puedo ayudarte?",
        "male": "¡Hola! Estoy listo. ¿En qué puedo ayudarte?",
    },
    "fr": {
        "female": "Bonjour ! Je suis prête. Comment puis-je vous aider ?",
        "male": "Bonjour ! Je suis prêt. Comment puis-je vous aider ?",
    },
    "de": _same("Hallo! Ich bin bereit. Wie kann ich Ihnen helfen?"),
}

DIDNT_CATCH = {
    "en": _same("Sorry, I didn't catch that — could you say it again?"),
    # "पाया"/"पाई" is the bot describing itself, so it follows the voice.
    # "कह सकते हैं" is about the CALLER and stays as it is — आप takes the
    # plural-formal form regardless of who is being addressed.
    "hi": {
        "female": "माफ़ कीजिए, मैं समझ नहीं पाई — क्या आप दोबारा कह सकते हैं?",
        "male": "माफ़ कीजिए, मैं समझ नहीं पाया — क्या आप दोबारा कह सकते हैं?",
    },
    # Spanish compound past ("he entendido") does NOT agree with the subject,
    # so unlike the greeting there is genuinely nothing to vary here.
    "es": _same("Perdona, no te he entendido. ¿Puedes repetirlo?"),
    "fr": {
        "female": "Désolée, je n'ai pas compris. Pouvez-vous répéter ?",
        "male": "Désolé, je n'ai pas compris. Pouvez-vous répéter ?",
    },
    "de": _same("Entschuldigung, das habe ich nicht verstanden. Können Sie das wiederholen?"),
}

# Languages where the bot referring to ITSELF changes with its gender. The
# model writes far more speech than the two fixed strings above, so it needs
# telling as well — the same live call had it saying "मैं आपकी आवाज़ सुन रहा
# हूँ" (masculine) through a female voice.
GENDERED_SELF_REFERENCE = frozenset({"hi", "fr", "es"})

# Endonyms deliberately — the instruction reads more naturally to the model
# in the language it is being asked to speak, and it keeps the prompt
# self-consistent rather than describing Hindi to the model in English.
LANGUAGE_NAMES = {
    "en": "English",
    "hi": "Hindi (हिन्दी)",
    "es": "Spanish (español)",
    "fr": "French (français)",
    "de": "German (Deutsch)",
}


# Cartesia voices, chosen 2026-09-04 from their live catalogue (934 voices;
# 49 native Hindi, 68 French, 33 German, 79 Spanish) filtered to the ones
# Cartesia itself describes for customer-support / virtual-assistant work,
# which is what these bots do. Every id below was verified against the API:
# it exists, and its `language` field really is the language it is filed
# under here — a mistyped UUID would otherwise be a silent wrong-voice bug.
#
# The user picked the two Hindi voices by listening to them. The other
# languages are Cartesia's descriptions taken at face value and are the
# STANDARD regional variant in each case (France French, not Québécois;
# standard German, not Swiss-German; Spain Spanish, not Latin American) —
# Cartesia has all of those alternatives if a preference ever emerges.
#
# The English three are pre-existing and kept for continuity: bots already
# in the database point at them. Their old UI labels were wrong, though —
# "Atlas — Professional, confident male voice" is really Sierra, a Californian
# woman, and "Aria — Neutral" is really Greg, a man. Names here are the real
# ones from the API.
VOICES: dict[str, list[dict[str, str]]] = {
    "en": [
        {"id": "a0e99841-438c-4a64-b679-ae501e7d6091", "name": "Greg", "gender": "male"},
        {"id": "694f9389-aac1-45b6-b726-9d9369183238", "name": "Sarah", "gender": "female"},
        {"id": "b7d50908-b17c-442d-ad8d-810c63997ed9", "name": "Sierra", "gender": "female"},
    ],
    "hi": [
        {"id": "6b02ffe5-e3cb-48c0-a023-c72f85953375", "name": "Sneha", "gender": "female"},
        {"id": "adf97b9d-905c-41de-9fe9-afb387116d06", "name": "Vikas", "gender": "male"},
    ],
    "fr": [
        {"id": "e2ab5462-e7c8-492d-a244-41f39444af6e", "name": "Audrey", "gender": "female"},
        {"id": "cc4276e6-1ebc-429a-8c7d-930993d51abc", "name": "Julien", "gender": "male"},
    ],
    "de": [
        {"id": "38aabb6a-f52b-4fb0-a3d1-988518f4dc06", "name": "Alina", "gender": "female"},
        {"id": "e00dd3df-19e7-4cd4-827a-7ff6687b6954", "name": "Lukas", "gender": "male"},
    ],
    "es": [
        {"id": "de38f545-c574-44e8-9b54-a7d6fec1c6b1", "name": "Marta", "gender": "female"},
        {"id": "b0689631-eee7-4a6c-bb86-195f1d267c2e", "name": "Emilio", "gender": "male"},
    ],
}

# id -> the language that voice actually speaks. Built from VOICES so the two
# can never drift apart.
_VOICE_LANGUAGE = {v["id"]: lang for lang, vs in VOICES.items() for v in vs}


def _base(language: str | None) -> str:
    """Same normalisation as providers._base_lang — 'hi-IN' -> 'hi'."""
    return (language or "en").split("-")[0].lower()


def default_voice_for(language: str | None) -> str:
    """The voice a bot in this language should use if it has no better one."""
    return VOICES.get(_base(language), VOICES["en"])[0]["id"]


def resolve_voice(voice_id: str | None, language: str | None) -> str:
    """Correct a voice that speaks the wrong language.

    Until now the bot settings UI offered exactly three voices, all English,
    to every bot regardless of language. So a Hindi bot was necessarily
    assigned an English voice, and Cartesia — being multilingual — dutifully
    read Hindi words with English mouth-shapes. That is the accent the user
    asked about on 2026-09-04, and it is stored in every existing bot's
    voice_id right now, so fixing only the dropdown would leave every bot
    already in the database still wrong.

    The rule is deliberately narrow: correct a voice only when it is KNOWN to
    speak a different language. A voice_id absent from the catalogue is left
    exactly as it is — it may be a custom or cloned voice whose language this
    module has no business guessing at, and silently swapping someone's
    deliberate choice for a default would be a worse bug than the one being
    fixed.
    """
    spoken = _VOICE_LANGUAGE.get(voice_id or "")
    if spoken is None or spoken == _base(language):
        return voice_id or default_voice_for(language)
    return default_voice_for(language)


def voice_gender(voice_id: str | None, language: str | None = None) -> str:
    """"female" or "male" for a known voice, else the language default's.

    Falls back rather than returning None because every caller here has to
    pick one form or the other — there is no ungendered way to say "I can
    help you" in Hindi, so a None would only push the same guess upward.
    """
    for voice in VOICES.get(_VOICE_LANGUAGE.get(voice_id or "", ""), []):
        if voice["id"] == voice_id:
            return voice["gender"]
    return VOICES.get(_base(language), VOICES["en"])[0]["gender"]


def greeting_for(language: str | None, gender: str | None = None) -> str:
    """The line the bot opens the call with, in the voice's own gender."""
    forms = GREETINGS.get(_base(language), GREETINGS["en"])
    return forms.get(gender or _default_gender(language), forms["female"])


def didnt_catch_for(language: str | None, gender: str | None = None) -> str:
    """The fallback spoken when a turn produced no usable transcript."""
    forms = DIDNT_CATCH.get(_base(language), DIDNT_CATCH["en"])
    return forms.get(gender or _default_gender(language), forms["female"])


def _default_gender(language: str | None) -> str:
    """Whatever this language's default voice sounds like — so the wording
    matches the voice a bot actually gets when nobody chose one."""
    return VOICES.get(_base(language), VOICES["en"])[0]["gender"]


def system_language_note(language: str | None, gender: str | None = None) -> str:
    """The instruction that makes the model actually reply in the bot's
    language. Empty for English: English is already what an unprompted model
    defaults to, so the sentence would be pure prompt noise, and every token
    in a system prompt is one the model has to weigh on every single turn.

    Phrased to cover the mixed-language case seen live rather than just the
    clean one. Deepgram transcribes real Indian speech the way it is
    actually spoken — 'क्या आप मुझे sun पा रहे हैं?', Hindi with an English
    word in it — and a naive "always reply in Hindi" instruction leaves the
    model to guess what to do when its own input is half English. The caller
    borrowing an English word is not a request to switch languages.

    The gender clause matters more than it looks. The greeting and fallback
    are two fixed sentences; the model writes everything else, and on the
    live call it said "मैं आपकी आवाज़ सुन रहा हूँ" — masculine — through a
    female voice. Fixing only the fixed strings would leave the bot correct
    for one line and wrong for the rest of the conversation.
    """
    base = _base(language)
    if base == "en":
        return ""
    name = LANGUAGE_NAMES.get(base)
    if name is None:
        return ""
    note = (
        f"\n\nSpeak {name} — the caller has chosen it for this conversation. "
        f"Reply in {name} even when the caller mixes in English words, which "
        f"is normal in ordinary speech and is not a request to change "
        f"language. Only switch if they clearly ask you to."
    )
    if base in GENDERED_SELF_REFERENCE:
        speaking_as = gender or _default_gender(language)
        grammatical = "feminine" if speaking_as == "female" else "masculine"
        note += (
            f" The voice the caller hears is {speaking_as}, so use {grammatical} "
            f"grammatical forms whenever you refer to yourself."
        )
    return note
