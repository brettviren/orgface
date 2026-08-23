# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reading org text into the canonical model without Emacs.

The interesting cases are the ones the parser gets wrong on its own: see
the two promotions in :mod:`org_store.adapters.orgparser`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from org_store.adapters.orgparser import _parse_isolated, parse, parse_file
from org_store.extract import content_hash
from org_store.model import OrgDocument, walk
from org_store.store import StoreError

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


def test_block_content_that_looks_like_org_is_content() -> None:
    """A star line inside a block belongs to the block, not to the document.

    The parser ends the block at such a line on its own, which loses
    everything after it; the walker masks it first.
    """
    doc = parse("#+begin_src org\n* A heading\ntext\n#+end_src\n")
    (block,) = by_type(doc, "src-block")
    assert block.properties.value == "* A heading\ntext\n"
    assert by_type(doc, "headline") == []


def test_orgs_comma_escape_is_not_part_of_the_content() -> None:
    """``,*`` is how org writes a star line; the comma is not text."""
    doc = parse("#+begin_example\n,* A heading\n,,* Literal comma\n#+end_example\n")
    (block,) = by_type(doc, "example-block")
    assert block.properties.value == "* A heading\n,* Literal comma\n"


def test_a_keyword_line_inside_a_block_stays_inside_it() -> None:
    doc = parse("#+begin_example\n#+title: inside\n#+end_example\n")
    (block,) = by_type(doc, "example-block")
    assert block.properties.value == "#+title: inside\n"
    assert doc.properties.title == []


def test_ordered_items_keep_their_numbers() -> None:
    """org-parser reports the punctuation alone, so the counter is added."""
    doc = parse("3. three\n4. four\n")
    assert [item.properties.bullet for item in by_type(doc, "item")] == ["3.", "4."]


def test_a_bare_link_keeps_its_scheme() -> None:
    """The scheme is a separate field, and a link without it is a different one."""
    doc = parse("See https://example.org/x here.\n")
    (link,) = by_type(doc, "link")
    assert link.properties.get("raw_link") == "https://example.org/x"
    assert link.properties.get("type") == "https"
    assert link.properties.get("path") == "//example.org/x"


def test_an_angle_link_keeps_its_scheme() -> None:
    doc = parse("See <mailto:someone@example.org> here.\n")
    (link,) = by_type(doc, "link")
    assert link.properties.get("raw_link") == "mailto:someone@example.org"
    assert link.properties.get("format") == "angle"


def test_an_affiliated_keyword_stays_with_its_block() -> None:
    """``#+name:`` names the block after it, so its position matters."""
    doc = parse("#+name: snip\n#+begin_src sh\nls\n#+end_src\n")
    (section,) = doc.contents
    assert [node.type for node in section.contents] == ["keyword", "src-block"]
    assert section.contents[0].properties.get("key") == "NAME"


def test_a_caption_is_a_keyword_like_any_other() -> None:
    doc = parse("#+caption: A cap\n#+begin_src sh\nls\n#+end_src\n")
    (keyword,) = by_type(doc, "keyword")
    assert keyword.properties.get("key") == "CAPTION"
    assert keyword.properties.get("value") == "A cap"


def test_a_file_level_property_drawer_is_kept() -> None:
    """It is reported apart from the elements, and would otherwise be lost."""
    doc = parse("#+title: T\n:PROPERTIES:\n:ID: abc\n:END:\n\nBody.\n")
    (drawer,) = by_type(doc, "property-drawer")
    (property_node,) = drawer.contents
    assert property_node.properties.get("key") == "ID"
    assert property_node.properties.get("value") == "abc"


def test_document_keywords_keep_their_place() -> None:
    """A keyword is not hoisted to the top; only its own line says where it is."""
    doc = parse("#+title: T\n\nA paragraph.\n\n#+options: toc:2\n")
    (section,) = doc.contents
    assert [node.type for node in section.contents] == [
        "keyword",
        "paragraph",
        "keyword",
    ]


def test_a_special_block_keeps_its_name() -> None:
    """The name after ``#+begin_`` is all that distinguishes one."""
    doc = parse("#+begin_note :class warn\nSomething.\n#+end_note\n")
    (block,) = by_type(doc, "special-block")
    assert block.properties.get("type") == "note"
    assert block.properties.get("parameters") == ":class warn"


def test_indented_content_is_the_content_inside_it() -> None:
    """org-element has no indent element, so neither has the model."""
    doc = parse("* H\n\n  indented paragraph\n  second line\n")
    assert [node.type for node in by_type(doc, "paragraph")] == ["paragraph"]
    (paragraph,) = by_type(doc, "paragraph")
    assert paragraph.properties.get("indent") == 2
    assert "\n  " not in "".join(
        part for part in paragraph.contents if isinstance(part, str)
    )


def test_indented_lists_are_lists() -> None:
    doc = parse("* H\n\n  - one\n  - two\n")
    assert len(by_type(doc, "item")) == 2


def test_a_big_document_does_not_take_the_process_with_it() -> None:
    """org-parser's nodes outlive their tree unless the collector is held off.

    Reading one after the tree has gone reads freed memory and kills the
    process, which no ``except`` can catch -- so what this really checks
    is that :func:`org_store.adapters.orgparser.parse` still holds it off.
    """
    text = "#+title: Big\n\n" + "".join(
        f"* Heading {n}\n\nSome text with a [[https://example.org][link]] in it.\n\n"
        for n in range(400)
    )
    assert len(by_type(parse(text), "headline")) == 400


def test_a_description_item_keeps_its_tag() -> None:
    doc = parse("- term :: what it means\n")
    (item,) = by_type(doc, "item")
    assert item.properties.get("tag") == ["term"]


def test_a_description_list_after_a_paragraph_keeps_its_tag() -> None:
    """Which is where org-parser stops reporting one, and where they live."""
    doc = parse("Some prose.\n\n- term :: what it means\n- second :: another\n")
    first, second = by_type(doc, "item")
    assert first.properties.get("tag") == ["term"]
    assert second.properties.get("tag") == ["second"]


def test_a_plain_item_has_no_tag() -> None:
    doc = parse("Some prose.\n\n- an item with no tag in it\n")
    (item,) = by_type(doc, "item")
    assert item.properties.get("tag") is None


def test_an_items_wrapped_line_is_one_paragraph() -> None:
    """org-parser reports a line per paragraph; org sees one paragraph."""
    doc = parse("Some prose.\n\n- term :: text that runs\n  onto a second line\n")
    assert len(by_type(doc, "paragraph")) == 2  # the prose, and the item's own


def test_wrapped_lines_are_not_spaced_apart() -> None:
    """A separator where there is already a line break makes a blank line."""
    doc = parse("- term :: \n  first line,\n  second line\n")
    (paragraph,) = [
        node for node in by_type(doc, "paragraph") if node.contents
    ]
    assert "\n\n" not in "".join(
        part for part in paragraph.contents if isinstance(part, str)
    )


def test_item_continuation_lines_drop_the_bullet_indent() -> None:
    """The indent puts a line under the bullet; it is not what the item says."""
    doc = parse("- Item :: first line\n  second line\n  third line\n")
    text = "".join(
        part for node in by_type(doc, "paragraph") for part in node.contents
        if isinstance(part, str)
    )
    assert "\n " not in text


# -- reading a file safely --------------------------------------------------


def test_a_file_reads_the_same_either_way() -> None:
    """Isolating the parse must not change what comes back."""
    isolated = parse_file(DATA / "fixture.org")
    here = parse_file(DATA / "fixture.org", isolated=False)
    assert content_hash(isolated) == content_hash(here)


def _dies(text: str, sender) -> None:
    """A parser that dies the way tree-sitter does: no exception, no output."""
    import os

    os._exit(11)


def _raises(text: str, sender) -> None:
    sender.send(("error", "ValueError: something the child noticed"))
    sender.close()


def test_a_parser_that_dies_is_reported_rather_than_fatal() -> None:
    """A segmentation fault in the parser must not take the program with it."""
    with pytest.raises(StoreError, match="died reading this file"):
        _parse_isolated("* H\n", "probe.org", target=_dies)


def test_a_failure_in_the_child_reaches_the_caller() -> None:
    with pytest.raises(StoreError, match="something the child noticed"):
        _parse_isolated("* H\n", "probe.org", target=_raises)


def test_unknown_constructs_are_kept_not_dropped() -> None:
    """A construct with no mapping survives as its own text."""
    doc = parse("#+begin_weird\nsomething\n#+end_weird\n")
    assert list(walk(doc))


def test_empty_document() -> None:
    doc = parse("")
    assert doc.contents == []
    assert doc.properties.title == []
