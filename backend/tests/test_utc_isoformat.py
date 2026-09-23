"""Whole-branch review of 5.2 found one invitation showing two different
expiry times, and in IST two different DATES.

PyMongo hands back a NAIVE datetime for anything re-read from the
database (this Motor client has no tz_aware=True), so:

    POST /orgs/{id}/members  -> 2026-09-30T10:46:30.341431+00:00   (in memory)
    GET  /orgs/{id}/invitations -> 2026-09-30T10:46:30.341000      (from the DB)

The frontend's `new Date(iso)` reads a zoneless string as LOCAL time, so
the same invitation reads 10:46 in the pending list and 16:16 in the
panel above it. booking.py already had the fix for exactly this reason;
this promotes it to one shared helper so a third copy is never written.
"""

from datetime import UTC, datetime

from app.core.times import utc_isoformat


def test_an_aware_utc_datetime_keeps_saying_utc():
    dt = datetime(2026, 9, 30, 10, 46, 30, tzinfo=UTC)
    assert utc_isoformat(dt) == "2026-09-30T10:46:30+00:00"


def test_a_naive_datetime_from_the_database_is_labelled_utc_not_shifted():
    """The crucial one. .astimezone() would assume the OS's local zone and
    move the instant; this only attaches the label it already meant."""
    naive = datetime(2026, 9, 30, 10, 46, 30)
    assert utc_isoformat(naive) == "2026-09-30T10:46:30+00:00"


def test_both_forms_of_the_same_instant_produce_the_same_string():
    aware = datetime(2026, 9, 30, 10, 46, 30, tzinfo=UTC)
    naive = datetime(2026, 9, 30, 10, 46, 30)
    assert utc_isoformat(aware) == utc_isoformat(naive)


def test_none_stays_none():
    assert utc_isoformat(None) is None
