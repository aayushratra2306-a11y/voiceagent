"""2026-09-13 — the test suite dropped the production database.

To "verify" task 4.4 on the deployed server, this was run inside the live
backend container:

    docker compose exec backend python -m pytest tests/test_mongo_pool.py -v

conftest.py chose its database with

    os.environ.setdefault("DB_NAME", "voiceagent_test")

and its session fixture ends with `drop_database(database.name)`. setdefault
only fills a value that is MISSING, and the production container already has
DB_NAME=voiceagent, so the suite kept the production name and the teardown
deleted every account, bot, tool, document and transcript. Running one
harmless test file was enough, because the drop lives in an autouse fixture.
Atlas had no backup.

A quiet fallback is exactly the wrong shape for this: the safe default only
applied when nobody had configured anything. So the rule is now positive:
the suite refuses to start unless the database name says it is disposable,
and the drop re-checks before it runs. These tests pin both, and pin that
the test files never ship in the production image at all.
"""

import os
import subprocess
import sys
from pathlib import Path

from app.core.db_safety import is_disposable_database

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


def test_test_and_ci_database_names_are_disposable():
    assert is_disposable_database("voiceagent_test")
    assert is_disposable_database("voiceagent_ci")
    assert is_disposable_database("anything_test")


def test_the_production_name_is_not():
    assert not is_disposable_database("voiceagent")


def test_near_misses_are_not_disposable():
    """Strict suffix, because the cost of a false 'safe' is the database."""
    for name in ("", "test", "test_voiceagent", "voiceagent_testing", "voiceagent-test",
                 "voiceagent_prod", "VOICEAGENT_TEST", " voiceagent_test"):
        assert not is_disposable_database(name), name


def test_the_suite_refuses_to_start_against_a_production_database_name():
    """The incident, replayed without the damage.

    Two independent reasons this cannot touch real data even if the guard
    were broken: `--collect-only` never runs fixtures (so no teardown, no
    drop), and MONGODB_URL points at a port nothing listens on. Checking the
    consequences of this test before writing it is the whole lesson of the
    incident it covers.
    """
    env = {
        **os.environ,
        "DB_NAME": "voiceagent",
        "MONGODB_URL": "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=500",
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "tests/test_database_safety.py", "-p", "no:cacheprovider"],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120,
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0, "the suite started against DB_NAME=voiceagent"
    assert "Refusing to run the test suite" in output, output[-2000:]


def test_the_teardown_rechecks_before_dropping():
    """Defence in depth: if a database name ever reaches the drop some other
    way than DB_NAME, the drop itself still refuses."""
    conftest = (BACKEND / "tests" / "conftest.py").read_text(encoding="utf-8")
    drop_at = conftest.index("drop_database(")
    assert "is_disposable_database(" in conftest[max(0, drop_at - 600):drop_at], (
        "drop_database is no longer guarded by a disposable-name check right before it"
    )


def test_test_files_never_ship_in_the_production_image():
    """If the tests are not in the image, nobody can run them on the server."""
    lines = {
        line.strip() for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert "backend/tests" in lines

    # And nothing the Dockerfile actually needs was excluded along with them.
    for needed in ("backend", "backend/", "backend/app", "backend/scripts",
                   "backend/requirements.txt", "backend/main.py", "*", "**"):
        assert needed not in lines, f".dockerignore excludes {needed}, which the image needs"
