# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The two reading engines, checked against each other.

Emacs is the authority on what org means, so ``ox-json``'s output is the
oracle and the standalone walker is the thing under test.  The fixtures
in ``tests/data`` are real exporter output (see the README there), which
is what lets this run with no Emacs at all -- the live daemon path is
covered separately, under the ``emacs`` marker.

Divergences are not failures by default.  The two engines will never
agree on everything, and pretending otherwise would mean either a
perpetually red suite or a walker contorted to match Emacs' internals.
What the tests below pin down is the part that has to agree: the
document's *content*.  Where the engines differ in structure, the
difference is named and asserted explicitly, so a new one shows up as a
failure rather than passing unnoticed.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

import pytest

from org_store.adapters.orgparser import parse_file
from org_store.model import OrgDocument, walk
from org_store.text import plain_text

DATA = Path(__file__).parent / "data"

#: Fixtures that exist in both forms, so the two can be compared.
PAIRS = sorted(
    path.stem.removesuffix(".ox-json")
    for path in DATA.glob("*.ox-json.json")
    if (DATA / f"{path.stem.removesuffix('.ox-json')}.org").exists()
)


def engines(name: str) -> tuple[OrgDocument, OrgDocument]:
    """The same fixture as Emacs read it, and as the walker read it."""
    emacs = OrgDocument.model_validate(
        json.loads((DATA / f"{name}.ox-json.json").read_text())
    )
    return emacs, parse_file(DATA / f"{name}.org")


def census(doc: OrgDocument) -> collections.Counter:
    return collections.Counter(node.type for node in walk(doc))


def test_the_corpus_exists_in_both_forms() -> None:
    assert PAIRS, "no fixture has both an .org and an .ox-json.json form"


# -- what has to agree ------------------------------------------------------


@pytest.mark.parametrize("name", PAIRS)
def test_document_keywords_agree(name: str) -> None:
    """The keywords that come from the file itself."""
    emacs, walker = engines(name)
    assert plain_text(walker.properties.title) == plain_text(emacs.properties.title)
    assert walker.properties.filetags == emacs.properties.filetags


def test_environment_keywords_are_emacs_only() -> None:
    """Some of ox-json's document properties are not in the file at all.

    ``creator``, ``email``, ``language`` and an unstated ``author`` come
    from the exporting Emacs, not from anything written down, so no
    reader of the file can supply them and the two engines cannot agree.
    Recorded here so the difference is a known one rather than a puzzle.
    """
    emacs, walker = engines("fixture-timestamps")
    assert emacs.properties.creator and emacs.properties.creator.startswith("Emacs")
    assert emacs.properties.language
    assert walker.properties.creator is None
    assert walker.properties.language is None
    # The fixture has no #+author, yet Emacs supplies one.
    assert emacs.properties.author and not walker.properties.author


@pytest.mark.parametrize("name", PAIRS)
def test_headlines_agree(name: str) -> None:
    """Titles, levels, TODO state, priority and tags, in order."""

    def headlines(doc):
        return [
            (
                node.properties.get("raw_value"),
                node.properties.get("level"),
                node.properties.get("todo_keyword"),
                str(node.properties.get("priority") or ""),
                list(node.properties.get("tags") or []),
            )
            for node in walk(doc)
            if node.type == "headline"
        ]

    emacs, walker = engines(name)
    assert headlines(walker) == headlines(emacs)


@pytest.mark.parametrize("name", PAIRS)
def test_tag_inheritance_agrees(name: str) -> None:
    """The index is built from these, so a difference would split search."""

    def inherited(doc):
        return [
            list(getattr(node, "tags_all", []))
            for node in walk(doc)
            if node.type == "headline"
        ]

    emacs, walker = engines(name)
    assert inherited(walker) == inherited(emacs)


@pytest.mark.parametrize("name", PAIRS)
def test_property_drawers_agree(name: str) -> None:
    def drawers(doc):
        return [
            dict(getattr(node, "drawer", {}) or {})
            for node in walk(doc)
            if node.type == "headline"
        ]

    emacs, walker = engines(name)
    assert drawers(walker) == drawers(emacs)


@pytest.mark.parametrize("name", PAIRS)
def test_planning_timestamps_agree(name: str) -> None:
    def planning(doc):
        out = []
        for node in walk(doc):
            for kind in ("scheduled", "deadline", "closed"):
                stamp = node.properties.get(kind)
                if stamp is not None:
                    out.append((kind, stamp.start, stamp.type))
        return out

    emacs, walker = engines(name)
    assert planning(walker) == planning(emacs)


@pytest.mark.parametrize("name", PAIRS)
def test_verbatim_values_agree(name: str) -> None:
    """Source blocks, examples and maths must come back character for character."""

    def values(doc, kinds):
        return [
            node.properties.get("value")
            for node in walk(doc)
            if node.type in kinds
        ]

    kinds = {"src-block", "example-block", "latex-fragment", "latex-environment"}
    emacs, walker = engines(name)
    assert values(walker, kinds) == values(emacs, kinds)


@pytest.mark.parametrize("name", PAIRS)
def test_links_agree(name: str) -> None:
    def links(doc):
        return [
            (node.properties.get("raw_link"), node.properties.get("type"))
            for node in walk(doc)
            if node.type == "link"
        ]

    emacs, walker = engines(name)
    assert links(walker) == links(emacs)


@pytest.mark.parametrize("name", PAIRS)
def test_the_promoted_types_agree(name: str) -> None:
    """The walker synthesises these; Emacs gets them from org-element."""
    promoted = {
        "latex-fragment",
        "latex-environment",
        "footnote-definition",
        "footnote-reference",
    }
    emacs, walker = engines(name)
    assert {k: v for k, v in census(walker).items() if k in promoted} == {
        k: v for k, v in census(emacs).items() if k in promoted
    }


@pytest.mark.parametrize("name", PAIRS)
def test_the_words_of_the_document_agree(name: str) -> None:
    """Whatever the structure, no text may be gained or lost."""

    def words(doc):
        # Flattened once from the root: summing over every node would
        # count a paragraph's text again for its section and its heading.
        return sorted(plain_text(doc.contents).split())

    emacs, walker = engines(name)
    assert words(walker) == words(emacs)


# -- where they differ, named --------------------------------------------


@pytest.mark.parametrize("name", PAIRS)
def test_only_the_known_divergence_remains(name: str) -> None:
    """Emacs wraps loose text in paragraphs where the walker does not.

    org-element gives a list item's text and a quote block's contents
    their own ``paragraph`` node; the walker keeps the item's text
    directly.  It costs nothing -- the text, and every other node, is
    identical either way, as the tests above assert -- so it is recorded
    rather than chased.

    This test fails when the engines start differing about anything else,
    which is the point of having it.
    """
    emacs, walker = engines(name)
    difference = census(emacs) - census(walker)
    reverse = census(walker) - census(emacs)
    assert set(difference) <= {"paragraph"}, difference
    assert not reverse, reverse


@pytest.mark.parametrize("name", PAIRS)
def test_node_counts_are_otherwise_identical(name: str) -> None:
    emacs, walker = engines(name)
    without_paragraphs = {
        kind: count for kind, count in census(emacs).items() if kind != "paragraph"
    }
    assert {
        kind: count for kind, count in census(walker).items() if kind != "paragraph"
    } == without_paragraphs
