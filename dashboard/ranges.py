"""The date window the Analytics page is asking about.

Both analytics routers (`stats_api.py`, reading Shopify; `insights_api.py`,
reading Postgres) take the same window and have to agree on it exactly --
otherwise the two tabs of one page silently answer about different fortnights.
Parsed once, here, rather than twice with a drifting copy of the rules.

Three presets and a custom range, and the presets stay presets: `days=30`
still means "the last 30 days ending today", which is what the overview page
and every existing caller ask for. A custom window is `start`/`end` as plain
`YYYY-MM-DD` dates, inclusive on both ends, so a single day is `start == end`
rather than a range nobody can express.

Bad input is refused, never repaired. `days=13` was already a 400 rather than
a silent clamp -- a header reading "30 days" has to be 30 days -- and the same
rule extends here: `start` after `end` is a 400, not a quiet swap, because the
swap would answer a question nobody asked and look like it worked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

#: The three presets the UI offers as buttons.
ALLOWED_RANGES = (7, 30, 90)

#: The longest custom window that will be served. Not a correctness limit --
#: `dashboard_stats.MAX_PAGES` already keeps a huge range from hanging the
#: page and reports `truncated` when it bites -- but a two-year request is
#: almost always a typo in a date field, and answering it slowly is worse than
#: refusing it quickly.
MAX_SPAN_DAYS = 366


class BadRange(ValueError):
    """The caller's window could not be used, with the reason a staff member
    (or a developer reading a 400) can act on."""


@dataclass(frozen=True)
class Window:
    #: Both inclusive, both plain dates. The time-of-day flooring is done by
    #: whoever queries -- `DateRange.as_query` for Shopify, `bounds()` below
    #: for Postgres -- so this stays the thing the *user* picked.
    start: date
    end: date
    #: Which preset button produced this, or None for a custom range. The page
    #: uses it to keep a button pressed; nothing computes from it.
    preset: int | None

    @property
    def days(self) -> int:
        """Inclusive day count -- a single day is 1, not 0."""
        return (self.end - self.start).days + 1

    def bounds(self) -> tuple[datetime, datetime]:
        """The window as aware UTC datetimes, `end` running to the last
        instant of its day. Everything comparing against a `DateTime(timezone=True)`
        column goes through this, so no query can accidentally cut the final
        day off at midnight."""
        start = datetime.combine(self.start, datetime.min.time(), tzinfo=UTC)
        end = datetime.combine(self.end, datetime.max.time(), tzinfo=UTC)
        return start, end

    def each_day(self) -> list[str]:
        """Every date in the window, in order, as `YYYY-MM-DD`.

        This -- not "the last N days counting back from today" -- is what a
        zero-filled chart series has to be built from. Anchoring on today
        drops every point of a historical window on the floor and draws a flat
        line of zeros across dates that had real activity.
        """
        return [
            (self.start + timedelta(days=offset)).isoformat()
            for offset in range(self.days)
        ]

    def as_payload(self) -> dict:
        return {
            "range_days": self.days,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "preset": self.preset,
        }


def window_for_days(days: int) -> Window:
    """A preset: `days` days ending today, today included."""
    today = datetime.now(UTC).date()
    return Window(start=today - timedelta(days=days - 1), end=today, preset=days)


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        raise BadRange(f"{field} must be a date as YYYY-MM-DD") from None


def parse(*, days: int | None = None, start: str | None = None, end: str | None = None) -> Window:
    """The window the request is asking about.

    `start`/`end` win when both are given; `days` is the preset fallback, and
    with nothing at all it is the 30-day preset the page opens on. Raises
    `BadRange` with a message meant to be handed straight back as a 400.
    """
    if start or end:
        if not (start and end):
            raise BadRange("start and end must be given together")
        first, last = _parse_date(start, "start"), _parse_date(end, "end")
        if first > last:
            raise BadRange("start must not be after end")
        span = (last - first).days + 1
        if span > MAX_SPAN_DAYS:
            raise BadRange(f"the range must be {MAX_SPAN_DAYS} days or fewer (asked for {span})")
        return Window(start=first, end=last, preset=None)

    if days is None:
        days = 30
    if days not in ALLOWED_RANGES:
        raise BadRange(f"days must be one of {ALLOWED_RANGES}, or pass start and end")
    return window_for_days(days)


__all__ = [
    "ALLOWED_RANGES",
    "MAX_SPAN_DAYS",
    "BadRange",
    "Window",
    "window_for_days",
    "parse",
]
