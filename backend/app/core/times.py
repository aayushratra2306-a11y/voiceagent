"""One way to write a UTC timestamp for an API response.

PyMongo hands back a NAIVE datetime for anything re-read from the
database — this Motor client has no `tz_aware=True` — while an object
this process just built is aware. Serialising both with plain
`.isoformat()` gives two different strings for the same instant, one
carrying "+00:00" and one carrying nothing. A consumer that reads a
zoneless string as local time (JavaScript's `new Date()` does exactly
that) then shows two different times, and across a day boundary two
different dates.

Found twice: first in booking.py, where a customer's webhook receiver
got a timestamp with no zone attached; then again in 5.2's invitation
expiry, where one invitation showed one date in the "invitation created"
panel and the previous date in the pending list below it.

`.astimezone(UTC)` is NOT the fix and is actively wrong here: called on
a naive value it assumes the OS's local zone and shifts the instant.
This only attaches the label the value already meant.
"""

from datetime import UTC, datetime


def utc_isoformat(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()
