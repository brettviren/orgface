# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The canonical model against real ``ox-json`` output.

The golden fixture is genuine exporter output rather than a transcription;
see ``tests/data/README.org`` for how it was made and why that matters.
"""

from __future__ import annotations

import json

import pytest

from org_store.model import (
    SCHEMA_VERSION,
    GenericNode,
    HeadlineNode,
    LinkNode,
    OrgDocument,
    SrcBlockNode,
    node_types,
    walk,
)



def by_type(doc: OrgDocument, kind: str) -> list:
    return [node for node in walk(doc) if node.type == kind]


def test_validates_real_export(doc: OrgDocument) -> None:
    assert doc.data_type == "org-document"
    assert doc.schema_version == SCHEMA_VERSION
    assert list(walk(doc))


def test_document_keywords(doc: OrgDocument) -> None:
    props = doc.properties
    assert props.title == ["Canonical model fixture"]
    assert props.author == ["Brett Viren"]
    # ox-json splits filetags for us; the Emacs-free path must match this.
    assert props.filetags == ["physics", "dune"]
    assert props.language == "en"


def test_headline_is_typed(doc: OrgDocument) -> None:
    headline = by_type(doc, "headline")[0]
    assert isinstance(headline, HeadlineNode)
    assert headline.properties.raw_value == "A headline with tags"
    assert headline.properties.todo_keyword == "TODO"
    assert headline.properties.priority == "A"
    assert headline.properties.tags == ["work", "urgent"]
    assert headline.properties.level == 1


def test_headline_keeps_node_level_extras(doc: OrgDocument) -> None:
    """``tags-all`` and ``drawer`` sit on the node, not in its properties."""
    headline = by_type(doc, "headline")[0]
    assert headline.tags_all == ["physics", "dune", "work", "urgent"]
    assert headline.drawer["CUSTOM_ID"] == "a-headline"
    assert headline.drawer["EFFORT"] == "2:00"


def test_planning_timestamps(doc: OrgDocument) -> None:
    headline = by_type(doc, "headline")[0]
    scheduled = headline.properties.scheduled
    deadline = headline.properties.deadline
    assert scheduled is not None and deadline is not None
    assert scheduled.data_type == "timestamp"
    assert scheduled.start == "2026-08-24"
    assert scheduled.type == "active"
    assert deadline.raw_value == "<2026-08-30 Sun>"
    # An absent repeater is present-but-null rather than missing.
    assert scheduled.repeater is not None
    assert scheduled.repeater.unit is None


def test_src_block(doc: OrgDocument) -> None:
    block = by_type(doc, "src-block")[0]
    assert isinstance(block, SrcBlockNode)
    assert block.properties.language == "python"
    assert block.properties.parameters == ":results output"
    assert block.properties.value == 'print("hello")\n'


def test_link(doc: OrgDocument) -> None:
    link = by_type(doc, "link")[0]
    assert isinstance(link, LinkNode)
    assert link.properties.type == "https"
    assert link.properties.raw_link == "https://orgmode.org"
    assert link.is_internal is False
    assert link.target_ref is None


def test_latex_is_captured_verbatim(doc: OrgDocument) -> None:
    """Math is stored as source, never as a parsed expression."""
    fragment = by_type(doc, "latex-fragment")[0]
    assert fragment.properties.value == "$E = mc^2$"
    assert fragment.properties.dialect == "latex"

    environment = by_type(doc, "latex-environment")[0]
    assert environment.properties.value.startswith("\\begin{equation}")
    assert environment.properties.value.endswith("\\end{equation}\n")


def test_footnotes(doc: OrgDocument) -> None:
    assert by_type(doc, "footnote-reference")[0].properties.label == "1"
    assert by_type(doc, "footnote-definition")[0].properties.label == "1"


def test_nested_emphasis_recurses(doc: OrgDocument) -> None:
    """Emphasis is a tree of nodes, not a flat set of marks."""
    bold = by_type(doc, "bold")[0]
    assert isinstance(bold, GenericNode)  # not in the typed set, still kept
    assert bold.contents == ["bold"]


def test_leaf_contents_default_to_empty(doc: OrgDocument) -> None:
    """``ox-json`` omits ``contents`` on leaves; the model always has it."""
    assert by_type(doc, "latex-fragment")[0].contents == []


def test_unknown_types_fall_back_to_generic() -> None:
    node = {
        "$$data_type": "org-node",
        "type": "horizontal-rule",
        "ref": "orgdeadbeef",
        "properties": {"post-blank": 1, "some-future-key": [1, 2]},
    }
    doc = OrgDocument.model_validate({"contents": [node]})
    (parsed,) = walk(doc)
    assert isinstance(parsed, GenericNode)
    assert parsed.type == "horizontal-rule"
    # Nothing is dropped just because the type is unrecognised.  Extras
    # keep the wire spelling; only declared fields get kebab aliases.
    assert parsed.properties.model_extra["some-future-key"] == [1, 2]
    assert parsed.properties.post_blank == 1


def test_typed_set_is_a_subset_of_org_element() -> None:
    """Typed classes exist for the types we operate on, not all of them."""
    typed = node_types()
    assert "headline" in typed and "src-block" in typed
    assert "bold" not in typed  # deliberately generic


def test_round_trip_is_stable(doc: OrgDocument) -> None:
    once = doc.dump()
    twice = OrgDocument.model_validate(once).dump()
    assert once == twice


def test_dump_uses_wire_names(doc: OrgDocument) -> None:
    dumped = doc.dump()
    assert dumped["$$data_type"] == "org-document"
    headline = next(
        node for node in _iter_dicts(dumped) if node.get("type") == "headline"
    )
    assert "raw-value" in headline["properties"]
    assert "raw_value" not in headline["properties"]
    assert "tags-all" in headline


def test_extra_document_keys_survive(fixture_json: dict) -> None:
    """A key the model does not declare is kept rather than dropped."""
    doc = OrgDocument.model_validate(
        {**fixture_json, "exporter-note": "hello"}
    )
    assert doc.dump()["exporter-note"] == "hello"


def _iter_dicts(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _iter_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_dicts(item)
