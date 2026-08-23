# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Turning a document into some other kind of text.

Real templates and a real document; the Emacs method is exercised with a
stand-in for the daemon, since what it has to get right on this side is
*which file it hands over*, not what org-export does with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from org_store.adapters.orgparser import parse, parse_file
from org_store.export import FORMATS, METHODS, ExportError, get_format, render

DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def fixture():
    return parse_file(DATA / "fixture.org", isolated=False)


# -- the registry -----------------------------------------------------------


def test_html_is_a_format() -> None:
    html = get_format("html")
    assert html.extension == ".html"
    assert html.exporter == "html"


def test_only_the_emacs_method_needs_emacs() -> None:
    assert METHODS["emacs"].needs_emacs
    assert not METHODS["template"].needs_emacs


def test_every_format_has_a_packaged_template() -> None:
    from org_store.export.template import TEMPLATES

    for fmt in FORMATS.values():
        assert (TEMPLATES / fmt.template).is_file(), fmt.name


def test_an_unknown_format_names_the_real_ones() -> None:
    with pytest.raises(ExportError, match="html"):
        render(parse("* H\n"), fmt="pdf")


def test_an_unknown_method_names_the_real_ones() -> None:
    with pytest.raises(ExportError, match="template"):
        render(parse("* H\n"), method="telepathy")


# -- the template method ----------------------------------------------------


def test_html_is_a_whole_page(fixture) -> None:
    page = render(fixture, fmt="html", doc_id="fixture.org")
    assert page.startswith("<!DOCTYPE html>")
    assert "<title>Canonical model fixture</title>" in page
    assert page.rstrip().endswith("</html>")


def test_headings_sit_below_the_title(fixture) -> None:
    """The page's own title is the h1, so a top level heading is an h2."""
    page = render(fixture, fmt="html", doc_id="fixture.org")
    assert '<h1 class="title">' in page
    assert "<h2>" in page


def test_text_from_the_document_is_escaped() -> None:
    doc = parse("#+title: T\n\nA <script>alert(1)</script> & more.\n")
    page = render(doc, fmt="html")
    assert "&lt;script&gt;" in page
    assert "<script>" not in page


def test_markup_becomes_markup() -> None:
    doc = parse("#+title: T\n\nSome *bold* and /italic/ and ~code~ here.\n")
    page = render(doc, fmt="html")
    assert "<strong>bold</strong>" in page
    assert "<em>italic</em>" in page
    assert "<code>code</code>" in page


def test_a_source_block_keeps_its_language() -> None:
    doc = parse("#+title: T\n\n#+begin_src python\nprint(1)\n#+end_src\n")
    page = render(doc, fmt="html")
    assert '<code class="language-python">' in page
    assert "print(1)" in page


def test_a_description_list_becomes_a_description_list() -> None:
    doc = parse("#+title: T\n\n- term :: what it means\n")
    page = render(doc, fmt="html")
    assert "<dt>term</dt>" in page
    assert "<dd>what it means</dd>" in page


def test_an_ordered_list_is_ordered() -> None:
    page = render(parse("#+title: T\n\n1. one\n2. two\n"), fmt="html")
    assert "<ol>" in page and "<li>one</li>" in page


def test_a_table_with_a_rule_gets_a_header_row() -> None:
    doc = parse("#+title: T\n\n| a | b |\n|---+---|\n| 1 | 2 |\n")
    page = render(doc, fmt="html")
    assert "<th>a</th>" in page
    assert "<td>1</td>" in page


def test_a_link_to_a_sibling_points_at_its_rendering() -> None:
    """A wiki rendered whole should be browsable, not a page of dead links."""
    doc = parse("#+title: T\n\nSee [[file:other.org][the other]].\n")
    page = render(doc, fmt="html")
    assert '<a href="other.html">the other</a>' in page


def test_an_outside_link_is_left_alone() -> None:
    doc = parse("#+title: T\n\nSee [[https://example.org][it]].\n")
    page = render(doc, fmt="html")
    assert '<a href="https://example.org">it</a>' in page


def test_document_metadata_is_not_content() -> None:
    """Keywords and drawers describe the document; they are not in it."""
    doc = parse("#+title: T\n#+filetags: :a:\n\n* H\n:PROPERTIES:\n:ID: x\n:END:\n")
    page = render(doc, fmt="html")
    assert "#+filetags" not in page
    assert ":PROPERTIES:" not in page


def test_a_template_of_ones_own_is_used(tmp_path) -> None:
    template = tmp_path / "plain.j2"
    template.write_text("{{ title }} has {{ document.contents|length }} part(s)\n")
    doc = parse("#+title: Mine\n\nText.\n")
    assert render(doc, template=template) == "Mine has 1 part(s)\n"


def test_a_template_that_is_not_there_is_reported(tmp_path) -> None:
    with pytest.raises(ExportError):
        render(parse("* H\n"), template=tmp_path / "absent.j2")


# -- the emacs method -------------------------------------------------------


class FakeEmacs:
    """Stands in for the daemon: records what it was asked to export."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.backend: str | None = None
        self.text: str = ""

    def export(self, path: Path, backend: str) -> str:
        self.path, self.backend = Path(path), backend
        self.text = Path(path).read_text(encoding="utf-8")
        return f"<html>{backend}</html>"


def test_the_emacs_method_exports_the_stored_file(tmp_path) -> None:
    """A document that is a file is exported where it lies.

    Not through a copy: relative links, includes and images have to
    resolve the way they do for a person opening it.
    """
    source = tmp_path / "notes.org"
    source.write_text("#+title: T\n\nText.\n")
    emacs = FakeEmacs()

    page = render(
        parse(source.read_text()),
        method="emacs",
        emacs=emacs,
        doc_id="notes.org",
        source=source,
    )
    assert page == "<html>html</html>"
    assert emacs.path == source


def test_a_document_with_no_file_gets_one() -> None:
    """A store that keeps no files still has something Emacs can open."""
    emacs = FakeEmacs()
    render(
        parse("#+title: Kept in sqlite\n\nText.\n"),
        method="emacs",
        emacs=emacs,
        doc_id="kept",
    )
    assert emacs.path is not None and emacs.path.suffix == ".org"
    assert "#+title: Kept in sqlite" in emacs.text
    assert not emacs.path.exists(), "the temporary file outlived the export"


def test_the_format_chooses_the_exporter(tmp_path) -> None:
    emacs = FakeEmacs()
    render(parse("* H\n"), fmt="html", method="emacs", emacs=emacs)
    assert emacs.backend == "html"


def test_the_emacs_method_says_when_it_has_no_emacs() -> None:
    with pytest.raises(ExportError, match="needs an Emacs"):
        render(parse("* H\n"), method="emacs")
