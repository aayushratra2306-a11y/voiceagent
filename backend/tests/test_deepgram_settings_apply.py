"""Guards against a real bug found 2026-09-03: `language`, `endpointing`
and `interim_results` were passed to DeepgramSTTService as bare keyword
arguments, which pipecat silently drops -- they belong on
`settings=DeepgramSTTSettings(...)` instead. Confirmed live: a bare
`language="hi"` produced `_settings.language == "en"`, meaning every
Hindi-configured bot was transcribed as English on the default cloud path.

This is the third occurrence of the same silent-extra-field-drop pattern
in this project (after Task 1.1's VAD params and PipelineParams' audio
fields), and unlike those two this one had a real, live product impact
rather than being a harmless no-op. A config value silently not applying
produces no error and no log line -- the only way to catch it is to
inspect the constructed object directly, which is what this test does
against the real factory function, not a mock.
"""

from app.pipeline.providers import get_stt_service


def test_language_actually_reaches_the_deepgram_connection():
    svc = get_stt_service("hi")
    assert svc._settings.language == "hi", (
        "language was accepted by get_stt_service() but never reached the "
        "Deepgram connection -- every Hindi-configured bot would be "
        "transcribed as English"
    )


def test_english_is_still_the_default():
    svc = get_stt_service("en")
    assert svc._settings.language == "en"


def test_endpointing_actually_reaches_the_deepgram_connection():
    svc = get_stt_service("en")
    assert svc._settings.endpointing == 500, (
        "endpointing was silently dropped -- Deepgram falls back to its "
        "own undocumented server-side default instead"
    )


def test_interim_results_actually_reaches_the_deepgram_connection():
    svc = get_stt_service("en")
    assert svc._settings.interim_results is True


def test_ttfs_p99_latency_still_lands_as_a_plain_init_kwarg():
    # Unlike the three above, this one genuinely is a top-level constructor
    # parameter on DeepgramSTTService (confirmed by reading its __init__
    # directly) -- included here so the same test file documents which
    # parameters need `settings=` and which don't, rather than leaving that
    # distinction only in a comment someone could miss.
    svc = get_stt_service("en")
    assert svc._ttfs_p99_latency == 0.7
