# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""git-style date specifications for --since/--after/--until/--before.

Supports the forms people actually type at ``git log``: ISO dates and
timestamps, the ``N units ago`` family (with git's dotted spelling), and a
handful of named days.  It is deliberately a documented subset rather than
a re-implementation of git's ``approxidate``.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta


class DateError(Exception):
    """A date specification could not be understood."""


_UNITS = {
    "second": 1,
    "sec": 1,
    "s": 1,
    "minute": 60,
    "min": 60,
    "hour": 3600,
    "hr": 3600,
    "h": 3600,
    "day": 86400,
    "d": 86400,
    "week": 604800,
    "w": 604800,
    "fortnight": 1209600,
}

_MONTHS = {"month": 1, "year": 12}

_ISO_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y%m%d",
)

_RELATIVE_RE = re.compile(r"^(?:(\d+)\s+)?([a-z]+)(?:\s+ago)?$")

_LAST_RE = re.compile(r"^last\s+([a-z]+)$")


def _seconds_per(unit: str) -> int | None:
    """Seconds in UNIT, tolerating a plural spelling."""
    if unit in _UNITS:
        return _UNITS[unit]
    if unit.endswith("s") and unit[:-1] in _UNITS:
        return _UNITS[unit[:-1]]
    return None


def _months_per(unit: str) -> int | None:
    """Months in UNIT, tolerating a plural spelling."""
    if unit in _MONTHS:
        return _MONTHS[unit]
    if unit.endswith("s") and unit[:-1] in _MONTHS:
        return _MONTHS[unit[:-1]]
    return None


def _shift_months(moment: datetime, months: int) -> datetime:
    """Move MOMENT back by MONTHS, clamping to the end of short months."""
    total = moment.year * 12 + (moment.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def parse(spec: str, now: datetime | None = None) -> datetime:
    """Parse a git-style date specification into a datetime."""
    now = now or datetime.now()
    text = " ".join(spec.strip().replace(".", " ").split()).lower()
    if not text:
        raise DateError("empty date")

    if text == "now":
        return now
    if text == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "yesterday":
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight - timedelta(days=1)

    for fmt in _ISO_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    last = _LAST_RE.match(text)
    if last:
        unit = last.group(1)
        seconds = _seconds_per(unit)
        if seconds is not None:
            return now - timedelta(seconds=seconds)
        months = _months_per(unit)
        if months is not None:
            return _shift_months(now, months)

    match = _RELATIVE_RE.match(text)
    if match:
        count_text, unit = match.groups()
        # A bare "week ago" means one unit back, as git accepts.
        count = int(count_text) if count_text else 1
        seconds = _seconds_per(unit)
        if seconds is not None:
            return now - timedelta(seconds=count * seconds)
        months = _months_per(unit)
        if months is not None:
            return _shift_months(now, count * months)

    raise DateError(
        f"unrecognised date: {spec!r}\n"
        "expected YYYY-MM-DD, 'today', 'yesterday', or "
        "'N days/weeks/months/years ago'"
    )


def parse_bound(
    spec: str | None, now: datetime | None = None, end_of_day: bool = False
) -> datetime | None:
    """Parse an optional bound, optionally extending bare dates to day end.

    ``--until 2026-07-30`` should include everything written that day, so an
    upper bound given without a time is pushed to 23:59:59.999999.
    """
    if spec is None:
        return None
    moment = parse(spec, now=now)
    if end_of_day and _is_bare_day(spec):
        return moment.replace(hour=23, minute=59, second=59, microsecond=999999)
    return moment


def _is_bare_day(spec: str) -> bool:
    """True when SPEC names a day without a time of day."""
    text = " ".join(spec.strip().replace(".", " ").split()).lower()
    if text in {"today", "yesterday"}:
        return True
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            datetime.strptime(text, fmt)
            return True
        except ValueError:
            continue
    return False


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """The half-open-feeling inclusive [start, end] of a calendar day."""
    start = datetime(day.year, day.month, day.day)
    end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
    return start, end
