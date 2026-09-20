"""Every model the call path queries must be registered in the call worker's
own init_db() call.

This file exists because of a bug that survived from task 2.4 to task 3.5's
live testing on 2026-09-07, unnoticed by 368 passing tests.

A call runs in its OWN spawned OS process (app/pipeline/call_worker.py), and
Beanie's model registration is per-process. That process called init_db()
with its own, deliberately shorter list of models than main.py's. `Bot` had
never been on it — nothing inside a call needed to READ a Bot document,
because a bot's configuration crosses the process boundary as a plain dict
(bot_config in api/connect.py).

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
there, and asserts each one is registered for the call worker. That is the
actual invariant, and it holds regardless of which process the test itself
runs in.

Task 5.1 moved call_worker.py off its own hand-maintained list onto the
shared `app.models.registry.ALL_MODELS` (the same list main.py and
tests/conftest.py use) — a hand-maintained per-file copy is exactly what let
`Bot` go missing unnoticed in the first place. So this file's job changed
from "does call_worker.py's own list cover the call path" to two narrower
questions that add back up to the same guarantee: (1) does call_worker.py
still actually wire itself to that shared list rather than reintroducing its
own, and (2) does that shared list itself cover everything the call path
queries. Regex-parsing a literal `init_db([...])` list no longer applies —
there is no literal list left to parse — so this reads the registry import
instead.
"""

import ast
import re
from pathlib import Path

from app.models.registry import ALL_MODELS

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


def _registered_model_names() -> set[str]:
    """The model names the call worker actually registers — via the shared
    ALL_MODELS list, not a list of its own."""
    return {m.__name__ for m in ALL_MODELS}


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


def test_call_worker_registers_from_the_shared_model_list():
    """A hand-maintained per-file model list is exactly what let `Bot` go
    missing unnoticed for weeks (see module docstring). This asserts
    call_worker.py did not grow one back: its init_db call must be wired to
    app.models.registry.ALL_MODELS, the same list every other entry point
    uses, not a literal list of its own.
    """
    source = CALL_WORKER.read_text(encoding="utf-8")
    assert "from app.models.registry import ALL_MODELS" in source, (
        "call_worker.py should import ALL_MODELS from app.models.registry "
        "rather than importing individual models for its own init_db list."
    )
    assert re.search(r"await init_db\(\s*ALL_MODELS\s*\)", source), (
        "call_worker.py's init_db(...) call should be await init_db(ALL_MODELS) "
        "— found something else. A hand-rolled list here is the exact bug "
        "class this file exists to catch."
    )


def test_every_model_the_call_path_queries_is_registered_in_the_worker():
    """The regression guard for the 3.5 timezone bug.

    Reverting the `Bot` addition (now: removing `Bot` from ALL_MODELS, or
    call_worker.py registering something other than ALL_MODELS) makes this
    fail with the exact file that queries it — which is the information that
    was missing when this went unnoticed for weeks.
    """
    model_names = _model_names()
    assert "Bot" in model_names, "sanity: the Bot model should have been discovered"

    registered = _registered_model_names()
    queried = _models_queried_on_the_call_path(model_names)

    missing = {m: where for m, where in queried.items() if m not in registered}
    assert not missing, (
        "These models are queried by code that runs inside a spawned call "
        "worker, but are not in app.models.registry.ALL_MODELS (which "
        "call_worker.py registers via init_db(ALL_MODELS)). Beanie raises "
        "CollectionWasNotInitialized for each — and if the caller wraps the "
        "query in a try/except (as booking.get_config does), it will fail "
        "SILENTLY and fall back to defaults on every live call:\n"
        + "\n".join(f"  {m}  — queried in {where}" for m, where in sorted(missing.items()))
    )


def test_bot_specifically_is_registered():
    """Named separately so a failure reads as the actual 3.5 symptom rather
    than a generic drift message: without this, a bot's configured timezone
    and opening hours are silently ignored on every call."""
    assert "Bot" in _registered_model_names(), (
        "Bot is missing from app.models.registry.ALL_MODELS — booking.get_config() "
        "will fall back to UTC/09:00-18:00/30min for every bot on every call"
    )
