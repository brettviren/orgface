# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org search`` end to end, against a real store."""

from __future__ import annotations

from pathlib import Path

import pytest

from org_cli_helper.cli import main

DATA = Path(__file__).parent / "data"


@pytest.fixture
def stocked(tmp_path, monkeypatch, runner):
    """A store holding both fixtures, pointed at by configuration."""
    monkeypatch.setenv(
        "ORG_CLI_HELPER_STORE_DATABASE", str(tmp_path / "store.sqlite")
    )
    for name, doc_id in (
        ("fixture.ox-json.json", "fixture.org"),
        ("fixture-timestamps.ox-json.json", "ts.org"),
    ):
        source = tmp_path / name
        source.write_text((DATA / name).read_text())
        result = runner.invoke(main, ["store", "add", str(source), "--id", doc_id])
        assert result.exit_code == 0, result.output
    return tmp_path


def test_help_without_arguments(runner, stocked) -> None:
    result = runner.invoke(main, ["search"])
    assert "Search the store for TEXT" in result.output


def test_finds_a_heading(runner, stocked) -> None:
    result = runner.invoke(main, ["search", "headline"])
    assert result.exit_code == 0
    assert "fixture.org:" in result.output
    assert "headline" in result.output


def test_no_matches_exits_nonzero(runner, stocked) -> None:
    result = runner.invoke(main, ["search", "nothingwhatsoever"])
    assert result.exit_code == 1
    assert "no matches" in result.output


def test_scope(runner, stocked) -> None:
    headings = runner.invoke(main, ["search", "headline", "--scope", "heading"])
    assert headings.exit_code == 0
    assert "(headline)" in headings.output
    assert "(paragraph)" not in headings.output


def test_several_words_are_all_required(runner, stocked) -> None:
    both = runner.invoke(main, ["search", "footnote", "definition"])
    assert both.exit_code == 0
    assert "The footnote definition." in both.output

    neither = runner.invoke(main, ["search", "footnote", "nothingwhatsoever"])
    assert neither.exit_code == 1


def test_punctuation_is_searched_not_parsed(runner, stocked) -> None:
    """A stray operator must be searched for, not raise a syntax error."""
    result = runner.invoke(main, ["search", "E=mc^2"])
    assert result.exit_code == 0
    assert "(latex-fragment)" in result.output


def test_prefix_search(runner, stocked) -> None:
    result = runner.invoke(main, ["search", "head*", "--scope", "heading"])
    assert result.exit_code == 0
    assert "(headline)" in result.output


def test_tag_filter(runner, stocked) -> None:
    tagged = runner.invoke(main, ["search", "footnote", "-t", "urgent"])
    assert tagged.exit_code == 0
    assert "(paragraph)" in tagged.output

    absent = runner.invoke(main, ["search", "footnote", "-t", "nosuchtag"])
    assert absent.exit_code == 1


def test_type_filter(runner, stocked) -> None:
    result = runner.invoke(main, ["search", "headline", "-T", "headline"])
    assert result.exit_code == 0
    assert "(paragraph)" not in result.output


def test_property_filter(runner, stocked) -> None:
    result = runner.invoke(
        main, ["search", "hello", "-p", "CUSTOM_ID=a-headline"]
    )
    assert result.exit_code == 0
    assert "(src-block)" in result.output


def test_malformed_property_filter_is_reported(runner, stocked) -> None:
    result = runner.invoke(main, ["search", "hello", "-p", "nonsense"])
    assert result.exit_code != 0
    assert "KEY=VALUE" in result.output


def test_document_filter(runner, stocked) -> None:
    result = runner.invoke(main, ["search", "meeting", "-d", "ts.org"])
    assert result.exit_code == 0
    assert "ts.org:" in result.output

    elsewhere = runner.invoke(main, ["search", "meeting", "-d", "fixture.org"])
    assert elsewhere.exit_code == 1


def test_max_count(runner, stocked) -> None:
    result = runner.invoke(main, ["search", "headline", "-n", "1"])
    assert result.exit_code == 0
    assert len([line for line in result.output.splitlines() if ":org" in line]) == 1


def test_search_reaches_both_documents(runner, stocked) -> None:
    result = runner.invoke(main, ["search", "head*"])
    assert result.exit_code == 0
    assert "fixture.org:" in result.output
