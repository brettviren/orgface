# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Parsing org-roam daily notes into log entries."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from orgface.dailies import (
    DailiesError,
    collect,
    daily_files,
    daily_path,
    parse_note,
)

NOTE = """\
:PROPERTIES:
:ID:       068075f1-de9f-4679-b5e5-ad0ab32d4805
:END:
#+title: 2026-07-30
* Some things since last week
<2026-07-30 Thu 13:32>

How do I keep up with dailies???!
* wcgpu1 in crisis again
<2026-07-30 Thu 15:17>

At 14:20 the RAM usage spiked.
** A subheading stays in the body
More text.
"""


@pytest.fixture
def dailies(tmp_path):
    directory = tmp_path / "daily"
    directory.mkdir()
    (directory / "2026-07-30.org").write_text(NOTE, encoding="utf-8")
    (directory / "2026-07-31.org").write_text(
        ":PROPERTIES:\n:ID:       328e1d40\n:END:\n"
        "#+title: 2026-07-31\n"
        "* Rules\n<2026-07-31 Fri 06:17>\nQuote goes here.\n",
        encoding="utf-8",
    )
    # Emacs backups and non-daily notes must be ignored.
    (directory / "2026-07-29.org~").write_text("* nope\n", encoding="utf-8")
    (directory / "notes.org").write_text("* nope\n", encoding="utf-8")
    (directory / "2026-02-31.org").write_text("* nope\n", encoding="utf-8")
    return directory


class TestDailyFiles:
    def test_only_daily_notes_oldest_first(self, dailies):
        found = daily_files(dailies)
        assert [day for day, _ in found] == [date(2026, 7, 30), date(2026, 7, 31)]

    def test_missing_directory(self, tmp_path):
        with pytest.raises(DailiesError):
            daily_files(tmp_path / "absent")


class TestDailyPath:
    def test_names_the_file_regardless_of_existence(self, tmp_path):
        assert daily_path(tmp_path, date(2026, 7, 30)) == tmp_path / "2026-07-30.org"


class TestParseNote:
    def test_headings_become_entries(self, dailies):
        note = parse_note(dailies / "2026-07-30.org")
        assert note.title == "2026-07-30"
        assert note.node_id == "068075f1-de9f-4679-b5e5-ad0ab32d4805"
        assert [e.heading for e in note.entries] == [
            "Some things since last week",
            "wcgpu1 in crisis again",
        ]

    def test_timestamp_is_read_from_the_body(self, dailies):
        note = parse_note(dailies / "2026-07-30.org")
        assert note.entries[0].timestamp == datetime(2026, 7, 30, 13, 32)

    def test_timestamp_only_line_is_promoted_out_of_the_body(self, dailies):
        """It is the entry's date, so reporting it twice would be noise."""
        entry = parse_note(dailies / "2026-07-30.org").entries[0]
        assert entry.timestamp == datetime(2026, 7, 30, 13, 32)
        assert entry.body == "How do I keep up with dailies???!"

    def test_inline_timestamp_stays_in_the_body(self, tmp_path):
        path = tmp_path / "2026-07-01.org"
        path.write_text(
            "* Meeting\nWe met at <2026-07-01 Wed 14:00> in the lab.\n",
            encoding="utf-8",
        )
        entry = parse_note(path).entries[0]
        assert entry.timestamp == datetime(2026, 7, 1, 14, 0)
        assert "<2026-07-01 Wed 14:00>" in entry.body

    def test_inactive_timestamp_line_is_promoted(self, tmp_path):
        path = tmp_path / "2026-07-01.org"
        path.write_text("* Thing\n[2026-07-01 Wed 08:05]\nBody.\n", encoding="utf-8")
        entry = parse_note(path).entries[0]
        assert entry.timestamp == datetime(2026, 7, 1, 8, 5)
        assert entry.body == "Body."

    def test_drawer_then_timestamp_are_both_promoted(self, tmp_path):
        path = tmp_path / "2026-07-01.org"
        path.write_text(
            "* Thing\n:PROPERTIES:\n:ID:       zz\n:END:\n"
            "<2026-07-01 Wed 08:05>\n\nBody.\n",
            encoding="utf-8",
        )
        entry = parse_note(path).entries[0]
        assert entry.node_id == "zz"
        assert entry.timestamp == datetime(2026, 7, 1, 8, 5)
        assert entry.body == "Body."

    def test_subheadings_stay_with_their_parent(self, dailies):
        note = parse_note(dailies / "2026-07-30.org")
        assert "A subheading stays in the body" in note.entries[1].body

    def test_entry_identity(self, dailies):
        note = parse_note(dailies / "2026-07-30.org")
        assert note.entries[1].ident == "068075f1-de9f-4679-b5e5-ad0ab32d4805#1"
        assert note.entries[1].short_ident() == "068075f1#1"

    def test_preamble_is_kept_as_an_entry(self, tmp_path):
        path = tmp_path / "2026-07-01.org"
        path.write_text(
            "#+title: 2026-07-01\nLoose text before any heading.\n* A heading\nBody.\n",
            encoding="utf-8",
        )
        note = parse_note(path)
        assert note.entries[0].body == "Loose text before any heading."
        assert note.entries[0].level == 0
        assert note.entries[1].heading == "A heading"

    def test_todo_and_tags_are_split_out(self, tmp_path):
        path = tmp_path / "2026-07-01.org"
        path.write_text("* TODO Buy milk :errand:home:\nBody.\n", encoding="utf-8")
        entry = parse_note(path).entries[0]
        assert entry.todo == "TODO"
        assert entry.heading == "Buy milk"
        assert entry.tags == ("errand", "home")

    def test_heading_property_drawer_is_not_body(self, tmp_path):
        path = tmp_path / "2026-07-01.org"
        path.write_text(
            "* Heading\n:PROPERTIES:\n:ID:       abc\n:END:\nBody.\n",
            encoding="utf-8",
        )
        entry = parse_note(path).entries[0]
        assert entry.body == "Body."
        assert entry.node_id == "abc"

    def test_entry_without_timestamp_falls_back_to_the_day(self, tmp_path):
        path = tmp_path / "2026-07-01.org"
        path.write_text("* No stamp\nBody.\n", encoding="utf-8")
        entry = parse_note(path).entries[0]
        assert entry.timestamp is None
        assert entry.when == datetime(2026, 7, 1)

    def test_non_daily_name_is_rejected(self, tmp_path):
        path = tmp_path / "notes.org"
        path.write_text("* x\n", encoding="utf-8")
        with pytest.raises(DailiesError):
            parse_note(path)


class TestCollect:
    def test_newest_first(self, dailies):
        entries = collect(dailies)
        assert [e.heading for e in entries] == [
            "Rules",
            "wcgpu1 in crisis again",
            "Some things since last week",
        ]

    def test_reverse(self, dailies):
        entries = collect(dailies, reverse=True)
        assert entries[0].heading == "Some things since last week"

    def test_limit(self, dailies):
        assert len(collect(dailies, limit=2)) == 2

    def test_limit_with_reverse_keeps_the_newest(self, dailies):
        entries = collect(dailies, limit=2, reverse=True)
        assert [e.heading for e in entries] == [
            "wcgpu1 in crisis again",
            "Rules",
        ]

    def test_since_is_inclusive(self, dailies):
        entries = collect(dailies, since=datetime(2026, 7, 31))
        assert [e.heading for e in entries] == ["Rules"]

    def test_until_is_inclusive(self, dailies):
        entries = collect(
            dailies, until=datetime(2026, 7, 30, 23, 59, 59, 999999)
        )
        assert len(entries) == 2
        assert all(e.day == date(2026, 7, 30) for e in entries)

    def test_bounds_apply_within_a_day(self, dailies):
        entries = collect(dailies, since=datetime(2026, 7, 30, 14, 0))
        assert [e.heading for e in entries] == ["Rules", "wcgpu1 in crisis again"]

    def test_empty_range(self, dailies):
        assert collect(dailies, since=datetime(2027, 1, 1)) == []
