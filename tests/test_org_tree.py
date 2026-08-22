# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Addressing and editing nodes inside a document."""

from __future__ import annotations

import pytest

from org_store.model import OrgDocument, walk
from org_store.refs import assign_refs
from org_store.tree import TreeError, find, iter_locations, locate


def test_locations_cover_every_node(doc: OrgDocument) -> None:
    located = [spot.node for spot in iter_locations(doc)]
    # walk() only descends contents; iter_locations also enters properties.
    assert len(located) >= len(list(walk(doc)))
    assert all(node.type for node in located)


def test_parents_are_reported(doc: OrgDocument) -> None:
    by_node = {id(spot.node): spot for spot in iter_locations(doc)}
    headline = next(s for s in iter_locations(doc) if s.node.type == "headline")
    assert headline.parent is None  # a top-level headline
    child = next(
        s
        for s in iter_locations(doc)
        if s.parent is headline.node and s.node.type == "section"
    )
    assert by_node[id(child.node)].parent is headline.node


def test_find_by_ref(doc: OrgDocument) -> None:
    assigned = assign_refs(doc, "a.org")
    target = next(walk(assigned))
    assert find(assigned, target.ref).node is target


def test_find_missing_returns_none(doc: OrgDocument) -> None:
    assert find(doc, "orgnothing") is None


def test_locate_raises_for_missing(doc: OrgDocument) -> None:
    with pytest.raises(TreeError, match="orgnothing"):
        locate(doc, "orgnothing")


def _document() -> OrgDocument:
    return OrgDocument.model_validate(
        {
            "contents": [
                {"$$data_type": "org-node", "type": "paragraph", "contents": ["a"]},
                {"$$data_type": "org-node", "type": "paragraph", "contents": ["b"]},
                {"$$data_type": "org-node", "type": "paragraph", "contents": ["c"]},
            ]
        }
    )


def test_replace_in_contents() -> None:
    doc = assign_refs(_document(), "a")
    ref = doc.contents[1].ref
    replacement = OrgDocument.model_validate(
        {
            "contents": [
                {"$$data_type": "org-node", "type": "quote-block", "contents": ["B"]}
            ]
        }
    ).contents[0]

    locate(doc, ref).replace(replacement)
    assert [node.type for node in doc.contents] == [
        "paragraph",
        "quote-block",
        "paragraph",
    ]
    assert doc.contents[1].contents == ["B"]


def test_remove_from_contents_closes_the_gap() -> None:
    doc = assign_refs(_document(), "a")
    locate(doc, doc.contents[1].ref).remove()
    assert [node.contents for node in doc.contents] == [["a"], ["c"]]


def test_edit_a_node_inside_properties() -> None:
    """A headline's title is a property, and still addressable."""
    doc = assign_refs(
        OrgDocument.model_validate(
            {
                "contents": [
                    {
                        "$$data_type": "org-node",
                        "type": "headline",
                        "properties": {
                            "title": [
                                "loud ",
                                {
                                    "$$data_type": "org-node",
                                    "type": "bold",
                                    "contents": ["bit"],
                                },
                            ]
                        },
                    }
                ]
            }
        ),
        "a",
    )
    emphasis = doc.contents[0].properties.title[1]
    locate(doc, emphasis.ref).remove()
    assert doc.contents[0].properties.title == ["loud "]
