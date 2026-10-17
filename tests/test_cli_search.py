# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org search`` end to end, against a real store."""

from __future__ import annotations

from pathlib import Path

import pytest

from orgface.cli import main

DATA = Path(__file__).parent / "data"


@pytest.fixture
def stocked(tmp_path, monkeypatch, runner):
    """A store holding both fixtures, pointed at by configuration."""
    monkeypatch.setenv(
        "ORGFACE_STORE_DATABASE", str(tmp_path / "store.sqlite")
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


def test_scores_are_shown_only_when_asked() -> None:
    from org_store.search import SearchResult

    from orgface.ui import format_search_results

    hit = SearchResult(doc_id="a.org", ref="1", type="headline", score=0.87654)
    assert "0.877" not in format_search_results([hit])
    assert format_search_results([hit], scores=True).startswith("0.877  a.org:1")


# -- hyperlinks -------------------------------------------------------------


def test_a_document_with_a_file_is_linked(runner, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORGFACE_STORE_DATABASE", str(tmp_path / "store.sqlite"))
    source = tmp_path / "linked.org"
    source.write_text((DATA / "fixture.org").read_text())
    assert runner.invoke(main, ["store", "add", str(source)]).exit_code == 0

    result = runner.invoke(main, ["search", "--hyperlink=always", "footnote"])
    assert result.exit_code == 0, result.output
    assert f"\033]8;;file://" in result.output
    assert f"{source}\033\\{source}\033]8;;\033\\:org" in result.output


def test_links_are_off_unless_asked_outside_a_terminal(runner, stocked) -> None:
    result = runner.invoke(main, ["search", "footnote"])
    assert "\033]8" not in result.output


def test_an_id_with_no_file_is_not_linked(runner, stocked) -> None:
    result = runner.invoke(main, ["search", "--hyperlink=always", "footnote"])
    assert result.exit_code == 0, result.output
    assert "\033]8" not in result.output


class Terminal:
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(
    "environ,expected",
    [
        ({"TERM": "xterm-kitty"}, True),
        ({"TERM": "xterm-256color", "KITTY_WINDOW_ID": "1"}, True),
        ({"TERM": "xterm-256color", "TERM_PROGRAM": "WezTerm"}, True),
        ({"TERM": "xterm-256color", "VTE_VERSION": "7600"}, True),
        ({"TERM": "xterm-256color", "VTE_VERSION": "4800"}, False),
        ({"TERM": "xterm-256color"}, False),
        ({"TERM": "tmux-256color", "KITTY_WINDOW_ID": "1"}, False),
        ({"TERM": "xterm-kitty", "TMUX": "/tmp/tmux"}, False),
    ],
)
def test_auto_trusts_only_terminals_that_announce_themselves(
    environ, expected
) -> None:
    from orgface.ui import resolve_hyperlinks

    assert resolve_hyperlinks("auto", Terminal(), environ) is expected


def test_auto_is_off_in_a_pipe() -> None:
    import io

    from orgface.ui import resolve_hyperlinks

    assert not resolve_hyperlinks("auto", io.StringIO(), {"TERM": "xterm-kitty"})
    assert resolve_hyperlinks("always", io.StringIO(), {})
    assert not resolve_hyperlinks("never", Terminal(), {"TERM": "xterm-kitty"})


def test_a_relative_id_is_found_under_a_directory_store(tmp_path) -> None:
    from orgface.config import load
    from orgface.storage import document_file

    (tmp_path / "notes.org").write_text("* hi\n")
    config = load(
        overrides={"store_backend": "orgfs", "store_directory": str(tmp_path)},
        environ={},
    )
    assert document_file(config, "notes.org") == tmp_path / "notes.org"
    assert document_file(config, "absent.org") is None
