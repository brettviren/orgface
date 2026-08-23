# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Where ``org render`` decides its output belongs.

Pure path arithmetic, so none of this needs a store or a CLI runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from org_cli_helper.rendering import (
    DEFAULT_DIRECTORY,
    RenderError,
    destinations,
    resolve,
    write,
)

HTML = ".html"


# -- naming documents -------------------------------------------------------


def test_a_name_is_a_document_id() -> None:
    assert resolve(["a.org"], ["a.org", "b.org"]) == ["a.org"]


def test_a_name_without_the_suffix_still_finds_it() -> None:
    """Nobody should have to remember which form the store used."""
    assert resolve(["a"], ["a.org"]) == ["a.org"]


def test_order_follows_what_was_asked_for() -> None:
    assert resolve(["b", "a"], ["a.org", "b.org"]) == ["b.org", "a.org"]


def test_a_name_that_is_in_no_store_is_reported() -> None:
    with pytest.raises(RenderError, match="no such document"):
        resolve(["c"], ["a.org", "b.org"])


# -- one document -----------------------------------------------------------


def test_one_document_defaults_to_its_own_name_here() -> None:
    (only,) = destinations(["notes.org"], None, HTML)
    assert only.path == Path("notes.html")


def test_a_document_in_a_subdirectory_still_lands_here() -> None:
    """One document with nowhere named goes to the current directory."""
    (only,) = destinations(["sub/notes.org"], None, HTML)
    assert only.path == Path("notes.html")


def test_a_dash_means_standard_output() -> None:
    (only,) = destinations(["notes.org"], "-", HTML)
    assert only.to_stdout and only.path is None


def test_an_output_names_the_file() -> None:
    (only,) = destinations(["notes.org"], "elsewhere/page.html", HTML)
    assert only.path == Path("elsewhere/page.html")


def test_an_output_that_is_a_directory_holds_the_file(tmp_path) -> None:
    (only,) = destinations(["notes.org"], str(tmp_path), HTML)
    assert only.path == tmp_path / "notes.html"


# -- several documents ------------------------------------------------------


def test_several_documents_default_to_one_directory() -> None:
    plan = destinations(["a.org", "b.org"], None, HTML)
    assert [spot.path for spot in plan] == [
        DEFAULT_DIRECTORY / "a.html",
        DEFAULT_DIRECTORY / "b.html",
    ]


def test_several_documents_keep_their_layout() -> None:
    """A store laid out in subdirectories renders into the same shape."""
    (only,) = destinations(["sub/a.org", "b.org"], "out", HTML)[:1]
    assert only.path == Path("out/sub/a.html")


def test_several_documents_cannot_go_to_standard_output() -> None:
    with pytest.raises(RenderError, match="one named document at a time"):
        destinations(["a.org", "b.org"], "-", HTML)


def test_all_means_a_directory_however_few_documents_there_are() -> None:
    """Where a rendering lands follows from what was asked for."""
    (only,) = destinations(["a.org"], None, HTML, everything=True)
    assert only.path == DEFAULT_DIRECTORY / "a.html"


def test_an_output_that_is_a_file_is_not_a_directory(tmp_path) -> None:
    existing = tmp_path / "page.html"
    existing.write_text("")
    with pytest.raises(RenderError, match="needs a directory"):
        destinations(["a.org", "b.org"], str(existing), HTML)


def test_a_document_cannot_escape_the_output_directory() -> None:
    with pytest.raises(RenderError, match="outside"):
        destinations(["../elsewhere.org", "b.org"], "out", HTML)


def test_nothing_to_render_is_nothing_to_write() -> None:
    assert destinations([], None, HTML) == []


# -- writing ----------------------------------------------------------------


def test_writing_makes_the_directory_it_needs(tmp_path) -> None:
    (spot,) = destinations(["a.org"], str(tmp_path / "deep" / "page.html"), HTML)
    written = write(spot, "<html></html>")
    assert written.read_text() == "<html></html>"


def test_standard_output_is_not_a_file(tmp_path) -> None:
    (spot,) = destinations(["a.org"], "-", HTML)
    with pytest.raises(RenderError):
        write(spot, "text")
