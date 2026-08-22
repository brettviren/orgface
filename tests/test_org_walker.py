# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reading org text into the canonical model without Emacs.

The interesting cases are the ones the parser gets wrong on its own: see
the two promotions in :mod:`org_store.adapters.orgparser`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from org_store.adapters.orgparser import parse, parse_file
from org_store.model import OrgDocument, walk

DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def parsed() -> OrgDocument:
    return parse_file(DATA / "fixture.org")


def by_type(doc: OrgDocument, kind: str) -> list:
    return [node for node in walk(doc) if node.type == kind]


# -- document level ---------------------------------------------------------


def test_keywords_become_document_properties(parsed: OrgDocument) -> None:
    assert parsed.properties.title == ["Canonical model fixture"]
    assert parsed.properties.author == ["Brett Viren"]


def test_filetags_are_split(parsed: OrgDocument) -> None:
    """ox-json hands back a list; the raw ``:a:b:`` is normalised here."""
    assert parsed.properties.filetags == ["physics", "dune"]


def test_headline(parsed: OrgDocument) -> None:
    headline = by_type(parsed, "headline")[0]
    assert headline.properties.raw_value == "A headline with tags"
    assert headline.properties.level == 1
    assert headline.properties.todo_keyword == "TODO"
    assert headline.properties.priority == "A"


def test_tag_inheritance_matches_ox_json(parsed: OrgDocument) -> None:
    """The two engines must agree, or the index would differ by source."""
    headline = by_type(parsed, "headline")[0]
    assert headline.properties.tags == ["work", "urgent"]
    assert headline.tags_all == ["physics", "dune", "work", "urgent"]


def test_property_drawer_becomes_a_mapping(parsed: OrgDocument) -> None:
    headline = by_type(parsed, "headline")[0]
    assert headline.drawer == {"CUSTOM_ID": "a-headline", "EFFORT": "2:00"}


def test_planning_timestamps(parsed: OrgDocument) -> None:
    headline = by_type(parsed, "headline")[0]
    assert headline.properties.scheduled.start == "2026-08-24"
    assert headline.properties.scheduled.type == "active"
    assert headline.properties.deadline.start == "2026-08-30"

    closed = by_type(parsed, "headline")[1].properties.closed
    assert closed.start == "2026-08-21T14:03:00"
    assert closed.type == "inactive"


def test_alignment_padding_is_not_content(parsed: OrgDocument) -> None:
    """Org pads titles and cells out to a column; that is presentation."""
    assert parsed.properties.title == ["Canonical model fixture"]
    headline = by_type(parsed, "headline")[0]
    assert headline.properties.title == ["A headline with tags"]
    assert by_type(parsed, "table-cell")[0].contents == ["name"]


# -- the two repairs --------------------------------------------------------


def test_inline_maths_is_promoted(parsed: OrgDocument) -> None:
    """org-parser has no LaTeX type; a fragment survives as inert text."""
    (fragment,) = by_type(parsed, "latex-fragment")
    assert fragment.properties.value == "$E = mc^2$"
    assert fragment.properties.display == "inline"
    assert fragment.properties.dialect == "latex"


def test_display_maths_is_promoted(parsed: OrgDocument) -> None:
    (environment,) = by_type(parsed, "latex-environment")
    assert environment.properties.value.startswith("\\begin{equation}")
    # The trailing newline is part of the element, as it is for Emacs.
    assert environment.properties.value.endswith("\\end{equation}\n")
    assert environment.properties.display == "block"


def test_maths_is_not_mangled_into_entities(parsed: OrgDocument) -> None:
    r"""The parser reads ``\begin`` as an entity; the repair must win."""
    entities = [n.properties.get("name") for n in by_type(parsed, "entity")]
    assert "begin" not in entities
    assert "nabla" not in entities


@pytest.mark.parametrize(
    "source",
    [
        "text $a=b$ more",
        r"text \(a=b\) more",
        r"text \[a=b\] more",
    ],
)
def test_every_inline_maths_spelling(source: str) -> None:
    doc = parse(source + "\n")
    assert [n.type for n in walk(doc) if n.type == "latex-fragment"] == [
        "latex-fragment"
    ]


def test_a_lone_dollar_is_not_maths() -> None:
    doc = parse("it cost $5 and no more\n")
    assert not [n for n in walk(doc) if n.type == "latex-fragment"]


def test_footnote_definition_is_promoted(parsed: OrgDocument) -> None:
    """There is no block-level footnote type; it parses as a paragraph."""
    (definition,) = by_type(parsed, "footnote-definition")
    assert definition.properties.label == "1"
    assert "The footnote definition." in str(definition.contents[0].contents)


def test_footnote_reference_survives(parsed: OrgDocument) -> None:
    (reference,) = by_type(parsed, "footnote-reference")
    assert reference.properties.label == "1"


# -- ordinary constructs ----------------------------------------------------


def test_source_block(parsed: OrgDocument) -> None:
    (block,) = by_type(parsed, "src-block")
    assert block.properties.language == "python"
    assert block.properties.value.strip() == 'print("hello")'
    assert block.properties.parameters == ":results output"


def test_verbatim_blocks_keep_their_text(parsed: OrgDocument) -> None:
    assert by_type(parsed, "example-block")[0].properties.value.strip() == (
        "literal example"
    )
    assert by_type(parsed, "fixed-width")[0].properties.value.strip() == (
        "fixed width line"
    )


def test_emphasis_nests_rather_than_flattening(parsed: OrgDocument) -> None:
    assert by_type(parsed, "bold")[0].contents == ["bold"]
    assert by_type(parsed, "italic")[0].contents == ["italic"]


def test_link(parsed: OrgDocument) -> None:
    (link,) = by_type(parsed, "link")
    assert link.properties.raw_link == "https://orgmode.org"
    assert link.properties.type == "https"
    assert link.contents == ["link to org mode"]


def test_list_items_and_checkboxes(parsed: OrgDocument) -> None:
    checkboxes = [n.properties.checkbox for n in by_type(parsed, "item")]
    assert "on" in checkboxes
    assert "off" in checkboxes


def test_table(parsed: OrgDocument) -> None:
    rows = by_type(parsed, "table-row")
    assert any(row.properties.type == "rule" for row in rows)
    assert by_type(parsed, "table-cell")[1].contents == ["value"]


def test_unknown_constructs_are_kept_not_dropped() -> None:
    """A construct with no mapping survives as its own text."""
    doc = parse("#+begin_weird\nsomething\n#+end_weird\n")
    assert list(walk(doc))


def test_empty_document() -> None:
    doc = parse("")
    assert doc.contents == []
    assert doc.properties.title == []
