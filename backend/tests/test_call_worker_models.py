"""Every model the call path queries must be registered in the call worker's
own init_db() list.

This file exists because of a bug that survived from task 2.4 to task 3.5's
live testing on 2026-09-07, unnoticed by 368 passing tests.

A call runs in its OWN spawned OS process (app/pipeline/call_worker.py), and
Beanie's model registration is per-process. That process calls init_db() with
its own, deliberately shorter list of models than main.py's. `Bot` had never
been on it — nothing inside a call needed to READ a Bot document, because a
bot's configuration crosses the process boundary as a plain dict (bot_config
in api/connect.py).

Task 3.5's booking.get_config() broke that assumption: it queries
`Bot.get(...)` directly to read the bot's timezone and opening hours. In
production that raised CollectionWasNotInitialized on every single call. Its
own try/except caught it, logged a warning nobody was reading, and fell back
to BookingConfig()'s defaults — so a bot configured for Asia/Kolkata, 09:00
to 18:00 quietly offered UTC slots and spoke UTC times to every caller.
Nothing crashed. The caller just got the wrong answer.

**Why the existing tests all passed**: test_booking_template.py calls
get_config() from inside the pytest process, and tests/conftest.py's own
init_db() registers the full model list, `Bot` included. So Bot.get() works
perfectly there and returns the real configured timezone. The test asserts
correct behaviour and is right to — it simply cannot see the one condition
that matters, because a test process is not a spawned call worker.

So this is a STRUCTURAL check rather than a behavioural one: it reads the
source of the code that runs inside a call, finds every Beanie model queried
there, and asserts each one is registered in call_worker's init_db list. That
is the actual invariant, and it holds regardless of which process the test
itself runs in.
"""

import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
CALL_WORKER = BACKEND / "app" / "pipeline" / "call_worker.py"

# The code that runs INSIDE a spawned call worker process. app/services is
# included because tool_registry and rag are both called from the pipeline
# mid-call.
CALL_PATH_DIRS = [BACKEND / "app" / "pipeline", BACKEND / "app" / "services"]

# Beanie Document subclasses in this project, by class name. Read from the
# models package rather than hardcoded, so a model added later is covered
# without anyone remembering to update this file.
def _model_names() -> set[str]:
    names = set()
    for path in (BACKEND / "app" / "models").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                # `class Bot(Document)` and `class Document(BeanieDocument)`
                # — the models package aliases the import both ways.
                base_name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                if base_name in ("Document", "BeanieDocument"):
                    names.add(node.name)
    return names


def _registered_in_call_worker() -> set[str]:
    """The model names passed to init_db([...]) in call_worker.py."""
    source = CALL_WORKER.read_text(encoding="utf-8")
    match = re.search(r"await init_db\(\[(.*?)\]\)", source, re.DOTALL)
    assert match, "could not find the init_db([...]) call in call_worker.py"
    return {name.strip() for name in match.group(1).replace("\n", "").split(",") if name.strip()}


def _models_queried_on_the_call_path(model_names: set[str]) -> dict[str, str]:
    """model name -> the file that queries it, for every Beanie read/write
    reachable from inside a call worker."""
    # .get(, .find(, .find_one(, .find_all(, .insert(  — anything that needs
    # the collection to be initialized.
    pattern = re.compile(
        r"\b(" + "|".join(sorted(model_names)) + r")\s*\.\s*"
        r"(get|find|find_one|find_all|insert|insert_many|save|delete)\s*\(",
    )
    found: dict[str, str] = {}
    for directory in CALL_PATH_DIRS:
        for path in directory.rglob("*.py"):
            for model in pattern.findall(path.read_text(encoding="utf-8")):
                found.setdefault(model[0], str(path.relative_to(BACKEND)))
    return found


def test_every_model_the_call_path_queries_is_registered_in_the_worker():
    """The regression guard for the 3.5 timezone bug.

    Reverting the `Bot` addition in call_worker.py makes this fail with the
    exact file that queries it — which is the information that was missing
    when this went unnoticed for weeks.
    """
    model_names = _model_names()
    assert "Bot" in model_names, "sanity: the Bot model should have been discovered"

    registered = _registered_in_call_worker()
    queried = _models_queried_on_the_call_path(model_names)

    missing = {m: where for m, where in queried.items() if m not in registered}
    assert not missing, (
        "These models are queried by code that runs inside a spawned call "
        "worker, but are not registered in call_worker.py's init_db([...]) "
        "list. Beanie raises CollectionWasNotInitialized for each — and if "
        "the caller wraps the query in a try/except (as booking.get_config "
        "does), it will fail SILENTLY and fall back to defaults on every "
        "live call:\n"
        + "\n".join(f"  {m}  — queried in {where}" for m, where in sorted(missing.items()))
    )


def test_bot_specifically_is_registered():
    """Named separately so a failure reads as the actual 3.5 symptom rather
    than a generic drift message: without this, a bot's configured timezone
    and opening hours are silently ignored on every call."""
    assert "Bot" in _registered_in_call_worker(), (
        "Bot is missing from call_worker.py's init_db list — booking.get_config() "
        "will fall back to UTC/09:00-18:00/30min for every bot on every call"
    )
