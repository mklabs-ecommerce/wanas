"""Now, and the one thing that has to happen to every datetime read back out
of the database before it can be compared with now.

Every `*_at` column is `DateTime(timezone=True)` and every value written to
one comes from `utcnow()`, which is timezone-aware. That is only half a
guarantee, because it says nothing about what comes *back*:

* PostgreSQL stores the offset and hands back an aware datetime.
* SQLite has no datetime type at all. SQLAlchemy stores an ISO string and
  parses it back with no offset, so the same column reads back **naive** --
  and `aware - naive` is a `TypeError`, not a wrong answer.

So the bug this exists to remove only ever appears on SQLite, which is
exactly where the test suite runs and where local development happens: code
that is correct in production raises on the developer's machine, and code
written against SQLite carries a `.replace(tzinfo=...)` that nobody can tell
is still needed. It had already been open-coded in four places with four
slightly different comments before it was collected here.

Rule for anything reading a stored timestamp: wrap it in `as_aware` before
subtracting or comparing it against `utcnow()`. A naive value is read as UTC,
because UTC is the only thing that was ever written.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """The one clock. Aware, UTC, never `datetime.utcnow()` (which is naive)."""
    return datetime.now(UTC)


def as_aware(value: datetime | None) -> datetime | None:
    """A stored timestamp, guaranteed comparable with `utcnow()`.

    None passes through: an unset column is a real state (never notified, never
    seen) and the caller has to decide what it means, which is not something a
    default timestamp could answer for it.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
