# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""git-style date specification parsing."""

from __future__ import annotations

from datetime import datetime

import pytest

from orgface.dates import DateError, day_bounds, parse, parse_bound

NOW = datetime(2026, 7, 31, 14, 30, 0)


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("2026-07-01", datetime(2026, 7, 1)),
        ("2026/07/01", datetime(2026, 7, 1)),
        ("20260701", datetime(2026, 7, 1)),
        ("260701", datetime(2026, 7, 1)),
        ("2026-07-01 09:15", datetime(2026, 7, 1, 9, 15)),
        ("2026-07-01T09:15:30", datetime(2026, 7, 1, 9, 15, 30)),
    ],
)
def test_iso_forms(spec, expected):
    assert parse(spec, now=NOW) == expected


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("now", NOW),
        ("today", datetime(2026, 7, 31)),
        ("yesterday", datetime(2026, 7, 30)),
    ],
)
def test_named_days(spec, expected):
    assert parse(spec, now=NOW) == expected


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("2 days ago", datetime(2026, 7, 29, 14, 30)),
        ("2 weeks ago", datetime(2026, 7, 17, 14, 30)),
        ("1 week ago", datetime(2026, 7, 24, 14, 30)),
        ("3 hours ago", datetime(2026, 7, 31, 11, 30)),
        ("2.weeks.ago", datetime(2026, 7, 17, 14, 30)),
        ("last week", datetime(2026, 7, 24, 14, 30)),
        ("week ago", datetime(2026, 7, 24, 14, 30)),
    ],
)
def test_relative_forms(spec, expected):
    assert parse(spec, now=NOW) == expected


def test_month_arithmetic_is_calendar_aware():
    assert parse("1 month ago", now=NOW) == datetime(2026, 6, 30, 14, 30)
    assert parse("last year", now=NOW) == datetime(2025, 7, 31, 14, 30)


def test_month_arithmetic_clamps_short_months():
    assert parse("1 month ago", now=datetime(2026, 3, 31)) == datetime(2026, 2, 28)


@pytest.mark.parametrize("spec", ["", "   ", "next tuesday", "2026-13-01", "banana"])
def test_rejected(spec):
    with pytest.raises(DateError):
        parse(spec, now=NOW)


class TestBounds:
    def test_upper_bound_on_a_bare_day_covers_the_whole_day(self):
        bound = parse_bound("2026-07-30", now=NOW, end_of_day=True)
        assert bound == datetime(2026, 7, 30, 23, 59, 59, 999999)

    def test_upper_bound_with_a_time_is_exact(self):
        bound = parse_bound("2026-07-30 09:00", now=NOW, end_of_day=True)
        assert bound == datetime(2026, 7, 30, 9, 0)

    def test_lower_bound_on_a_bare_day_starts_at_midnight(self):
        assert parse_bound("2026-07-30", now=NOW) == datetime(2026, 7, 30)

    def test_none_passes_through(self):
        assert parse_bound(None, now=NOW) is None

    def test_yesterday_as_an_upper_bound_covers_all_of_it(self):
        bound = parse_bound("yesterday", now=NOW, end_of_day=True)
        assert bound == datetime(2026, 7, 30, 23, 59, 59, 999999)


def test_day_bounds():
    start, end = day_bounds(NOW.date())
    assert start == datetime(2026, 7, 31)
    assert end == datetime(2026, 7, 31, 23, 59, 59, 999999)
