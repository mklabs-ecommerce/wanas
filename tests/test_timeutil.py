"""`as_aware`: the guard between a stored timestamp and `utcnow()`.

Every `*_at` column is written from `utcnow()` (aware, UTC) but SQLite reads
it back naive, so the subtraction that is fine in production raises a
TypeError everywhere the suite and local development run. These are the two
shapes that has to hold in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from common.timeutil import as_aware, utcnow


def test_a_naive_stored_value_is_read_as_utc_and_stays_comparable():
    naive = datetime(2026, 8, 24, 12, 0, 0)  # what SQLite hands back
    aware = as_aware(naive)
    assert aware.tzinfo is not None
    assert aware == datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)
    # The whole point: this line is a TypeError without the call.
    assert isinstance(utcnow() - aware, timedelta)


def test_an_aware_value_is_left_exactly_as_it_is():
    """PostgreSQL returns aware values, sometimes at a non-UTC offset. Neither
    the instant nor the offset may be rewritten."""
    original = datetime(2026, 8, 24, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    assert as_aware(original) is original


def test_none_passes_through():
    """An unset column is a real state -- never notified, never seen -- and
    substituting a timestamp for it would silently invent an event."""
    assert as_aware(None) is None


def test_utcnow_is_aware():
    assert utcnow().tzinfo is not None
