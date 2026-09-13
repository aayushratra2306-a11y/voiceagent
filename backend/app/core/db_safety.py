"""Which databases may be destroyed.

Added after the 2026-09-13 incident, in which the test suite dropped the
production database (see tests/test_database_safety.py for the full story).

Deliberately dependency-free: tests/conftest.py imports this BEFORE any
settings are loaded, because loading settings is what opens a connection.
"""

# A strict suffix, not "contains test". Every mistake this check can make in
# the permissive direction costs a database, so a name has to say plainly
# that it is disposable.
DISPOSABLE_SUFFIXES = ("_test", "_ci")


def is_disposable_database(name: str) -> bool:
    return any(name.endswith(suffix) and len(name) > len(suffix) and name.strip() == name
               and name == name.lower()
               for suffix in DISPOSABLE_SUFFIXES)
