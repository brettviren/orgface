# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org render`` end to end.

A real org-file store, the real templates, and the real command; only
the Emacs method is left to the live tests, since it needs a daemon.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orgface.cli import main

PAGES = {
    "one.org": "#+title: The first\n\nText of the first, linking [[file:two.org][on]].\n",
    "two.org": "#+title: The second\n\n* A heading\n\nMore text.\n",
    "deep/three.org": "#+title: The third\n\nDeeper.\n",
}


@pytest.fixture
def store(tmp_path, monkeypatch) -> Path:
    """A store of org files, and a working directory to render into."""
    root = tmp_path / "notes"
    for name, text in PAGES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("ORGFACE_STORE_BACKEND", "orgfs")
    monkeypatch.setenv("ORGFACE_STORE_DIRECTORY", str(root))

    here = tmp_path / "cwd"
    here.mkdir()
    monkeypatch.chdir(here)
    return here


# -- asking for nothing -----------------------------------------------------


def test_no_documents_and_no_all_is_help(runner, store) -> None:
    result = runner.invoke(main, ["render"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_options_without_documents_are_still_help(runner, store) -> None:
    """Rendering nothing is never what was meant, whatever else was said."""
    result = runner.invoke(main, ["render", "-f", "html"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_help_flag_is_accepted(runner, store) -> None:
    result = runner.invoke(main, ["render", "-h"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


# -- one document -----------------------------------------------------------


def test_one_document_lands_in_the_current_directory(runner, store) -> None:
    result = runner.invoke(main, ["render", "one.org"])
    assert result.exit_code == 0, result.output
    page = (store / "one.html").read_text()
    assert "<title>The first</title>" in page
    assert "wrote one.html" in result.output


def test_a_name_without_the_suffix_works(runner, store) -> None:
    assert runner.invoke(main, ["render", "one"]).exit_code == 0
    assert (store / "one.html").is_file()


def test_a_dash_sends_it_to_standard_output(runner, store) -> None:
    result = runner.invoke(main, ["render", "one.org", "-o", "-"])
    assert result.exit_code == 0
    assert result.output.startswith("<!DOCTYPE html>")
    assert not list(store.glob("*.html")), "nothing should have been written"


def test_an_output_names_the_file(runner, store) -> None:
    result = runner.invoke(main, ["render", "one.org", "-o", "page.html"])
    assert result.exit_code == 0
    assert (store / "page.html").is_file()


def test_an_output_directory_holds_the_file(runner, store) -> None:
    (store / "site").mkdir()
    assert runner.invoke(main, ["render", "one.org", "-o", "site"]).exit_code == 0
    assert (store / "site" / "one.html").is_file()


def test_a_template_of_ones_own(runner, store, tmp_path) -> None:
    template = tmp_path / "plain.j2"
    template.write_text("just {{ title }}\n")
    result = runner.invoke(
        main, ["render", "one.org", "-t", str(template), "-o", "-"]
    )
    assert result.exit_code == 0
    assert result.output.strip() == "just The first"


# -- several documents ------------------------------------------------------


def test_all_renders_the_whole_store(runner, store) -> None:
    result = runner.invoke(main, ["render", "-a"])
    assert result.exit_code == 0, result.output
    assert (store / "output" / "one.html").is_file()
    assert (store / "output" / "two.html").is_file()
    assert "wrote 3 files" in result.output


def test_all_writes_to_a_directory_even_for_one_document(
    runner, store, tmp_path, monkeypatch
) -> None:
    """--all asked for a directory; how many documents there are is chance."""
    root = tmp_path / "one-note"
    root.mkdir()
    (root / "only.org").write_text("#+title: Only\n\nText.\n", encoding="utf-8")
    monkeypatch.setenv("ORGFACE_STORE_DIRECTORY", str(root))

    result = runner.invoke(main, ["render", "-a"])
    assert result.exit_code == 0, result.output
    assert (store / "output" / "only.html").is_file()


def test_documents_keep_their_layout(runner, store) -> None:
    """A store in subdirectories renders into the same subdirectories."""
    assert runner.invoke(main, ["render", "-a", "-o", "site"]).exit_code == 0
    assert (store / "site" / "deep" / "three.html").is_file()


def test_named_documents_go_to_a_directory_too(runner, store) -> None:
    result = runner.invoke(main, ["render", "one.org", "two.org", "-o", "site"])
    assert result.exit_code == 0
    assert sorted(p.name for p in (store / "site").iterdir()) == [
        "one.html",
        "two.html",
    ]


def test_links_between_documents_point_at_the_renderings(runner, store) -> None:
    runner.invoke(main, ["render", "-a"])
    assert 'href="two.html"' in (store / "output" / "one.html").read_text()


# -- what cannot be done ----------------------------------------------------


def test_several_documents_cannot_go_to_standard_output(runner, store) -> None:
    result = runner.invoke(main, ["render", "-a", "-o", "-"])
    assert result.exit_code != 0
    assert "one named document at a time" in result.output


def test_an_output_file_cannot_hold_several_documents(runner, store) -> None:
    (store / "page.html").write_text("")
    result = runner.invoke(main, ["render", "one.org", "two.org", "-o", "page.html"])
    assert result.exit_code != 0
    assert "needs a directory" in result.output


def test_an_unknown_document_is_reported_before_anything_is_written(
    runner, store
) -> None:
    result = runner.invoke(main, ["render", "one.org", "nope.org"])
    assert result.exit_code != 0
    assert "no such document" in result.output
    assert not list(store.glob("**/*.html"))


def test_all_and_named_documents_are_not_both(runner, store) -> None:
    result = runner.invoke(main, ["render", "-a", "one.org"])
    assert result.exit_code != 0


def test_a_template_is_for_the_template_method(runner, store, tmp_path) -> None:
    template = tmp_path / "plain.j2"
    template.write_text("{{ title }}\n")
    result = runner.invoke(
        main, ["render", "one.org", "-m", "emacs", "-t", str(template)]
    )
    assert result.exit_code != 0
    assert "--template is for" in result.output


def test_an_unknown_format_is_refused(runner, store) -> None:
    result = runner.invoke(main, ["render", "one.org", "-f", "pdf"])
    assert result.exit_code != 0
