"""Task 4.8 — the load test harness is not run against anything real by
this test (or by this session at all — see the script's own module
docstring for why: it costs real provider usage, and that decision belongs
to the account holder). What's covered is the one safety property that
matters if someone runs it carelessly: it refuses to make a single request
without the explicit confirmation flag.
"""

import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1]


def test_it_refuses_to_run_without_the_confirmation_flag():
    """The whole safety net. Every call this script makes is a real,
    billed call against real provider accounts — this must never fire by
    accident (a copy-pasted command missing one flag, a CI job that
    shouldn't be running it at all)."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.load_test",
         "--base-url", "http://localhost:8080", "--email", "a@b.com",
         "--password", "x", "--bot-id", "y"],
        cwd=SCRIPT_DIR, capture_output=True, text=True, timeout=15,
    )

    assert result.returncode == 1
    assert "real and billed" in result.stderr
    # Nothing here proves no network call was attempted directly, but the
    # refusal happening before argparse even needs a reachable base_url is
    # the point — "http://localhost:8080" above is never dialed because
    # nothing gets past the confirmation check.


def test_the_confirmation_flag_is_named_unambiguously():
    """A flag that could be added out of habit (--yes, --force) defeats the
    point. It has to actually say what it's confirming."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.load_test", "--help"],
        cwd=SCRIPT_DIR, capture_output=True, text=True, timeout=15,
    )
    assert "--i-understand-this-costs-real-provider-usage" in result.stdout
