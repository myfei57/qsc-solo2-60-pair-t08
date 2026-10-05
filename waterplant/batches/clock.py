"""Time and date helpers for batch shelf life rules.

Expiry is a calendar date (``YYYY-MM-DD``) while ledger timestamps are UTC
instants. Keeping both behind one injectable clock lets the alert rules be
tested deterministically without freezegun.
"""

from __future__ import annotations

import datetime as dt

DATE_FORMAT = "%Y-%m-%d"


class Clock:
    """The current instant and current day, overridable from tests."""

    def __init__(self, fixed_day: dt.date | None = None) -> None:
        self._fixed_day = fixed_day

    def now(self) -> str:
        instant = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        return instant.strftime("%Y-%m-%dT%H:%M:%SZ")

    def today(self) -> dt.date:
        if self._fixed_day is not None:
            return self._fixed_day
        return dt.datetime.now(dt.timezone.utc).date()

    def set_today(self, day: dt.date | str | None) -> dt.date:
        """Pin the clock for tests; ``None`` returns it to wall clock time."""

        if day is None:
            self._fixed_day = None
        elif isinstance(day, dt.date):
            self._fixed_day = day
        else:
            self._fixed_day = parse_date(day)
        return self.today()


def parse_date(value: str) -> dt.date:
    """Parse a ``YYYY-MM-DD`` expiry date with a clear validation error."""

    text = str(value or "").strip()
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"expiry_date must look like YYYY-MM-DD, got {value!r}") from exc
    return parsed


def date_text(day: dt.date) -> str:
    return day.strftime(DATE_FORMAT)
