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

GREETINGS = {
    "en": "Hello! I'm ready. How can I help you?",
    "hi": "नमस्ते! मैं तैयार हूँ। मैं आपकी क्या मदद कर सकता हूँ?",
    "es": "¡Hola! Estoy listo. ¿En qué puedo ayudarte?",
    "fr": "Bonjour ! Je suis prêt. Comment puis-je vous aider ?",
    "de": "Hallo! Ich bin bereit. Wie kann ich Ihnen helfen?",
}

DIDNT_CATCH = {
    "en": "Sorry, I didn't catch that — could you say it again?",
    "hi": "माफ़ कीजिए, मैं समझ नहीं पाया — क्या आप दोबारा कह सकते हैं?",
    "es": "Perdona, no te he entendido. ¿Puedes repetirlo?",
    "fr": "Désolé, je n'ai pas compris. Pouvez-vous répéter ?",
    "de": "Entschuldigung, das habe ich nicht verstanden. Können Sie das wiederholen?",
}

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


def _base(language: str | None) -> str:
    """Same normalisation as providers._base_lang — 'hi-IN' -> 'hi'."""
    return (language or "en").split("-")[0].lower()


def greeting_for(language: str | None) -> str:
    """The line the bot opens the call with."""
    return GREETINGS.get(_base(language), GREETINGS["en"])


def didnt_catch_for(language: str | None) -> str:
    """The fallback spoken when a turn produced no usable transcript."""
    return DIDNT_CATCH.get(_base(language), DIDNT_CATCH["en"])


def system_language_note(language: str | None) -> str:
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
    """
    base = _base(language)
    if base == "en":
        return ""
    name = LANGUAGE_NAMES.get(base)
    if name is None:
        return ""
    return (
        f"\n\nSpeak {name} — the caller has chosen it for this conversation. "
        f"Reply in {name} even when the caller mixes in English words, which "
        f"is normal in ordinary speech and is not a request to change "
        f"language. Only switch if they clearly ask you to."
    )
