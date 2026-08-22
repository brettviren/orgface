# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Node refs: deterministic, document-scoped, and computed on this side."""

from __future__ import annotations


from org_store.model import OrgDocument, walk
from org_store.extract import content_hash
from org_store.refs import REF_PREFIX, assign_refs, compute_ref
from org_store.tree import iter_locations



def test_ref_is_a_pure_function_of_path() -> None:
    assert compute_ref("a.org", (0, 1)) == compute_ref("a.org", (0, 1))
    assert compute_ref("a.org", (0, 1)) != compute_ref("a.org", (1, 0))
    assert compute_ref("a.org", (0,)) != compute_ref("b.org", (0,))


def test_path_separator_cannot_be_forged() -> None:
    """Distinct paths must not collapse to the same hashed string."""
    assert compute_ref("d", ("a", "b")) != compute_ref("d", ("a/b",))
    assert compute_ref("d", (1, 2)) != compute_ref("d", (12,))


def test_ref_shape() -> None:
    ref = compute_ref("a.org", (0,))
    assert ref.startswith(REF_PREFIX)
    assert ref[len(REF_PREFIX) :].isalnum()


def test_every_node_gets_a_ref(doc: OrgDocument) -> None:
    assigned = assign_refs(doc, "fixture.org")
    assert all(spot.node.ref is not None for spot in iter_locations(assigned))


def test_refs_are_unique_within_a_document(doc: OrgDocument) -> None:
    assigned = assign_refs(doc, "fixture.org")
    refs = [spot.node.ref for spot in iter_locations(assigned)]
    assert len(refs) == len(set(refs))
    assert len(refs) > 50  # the fixture is not trivial


def test_assignment_is_reproducible(doc: OrgDocument) -> None:
    assert assign_refs(doc, "a.org").dump() == assign_refs(doc, "a.org").dump()


def test_refs_are_document_scoped(doc: OrgDocument) -> None:
    """Two documents of identical structure must not share refs."""
    assert assign_refs(doc, "a.org").dump() != assign_refs(doc, "b.org").dump()


def test_source_refs_are_replaced(doc: OrgDocument) -> None:
    """Whatever the exporting engine called a node, we rename it."""
    original = {node.ref for node in walk(doc)}
    assigned = {node.ref for node in walk(assign_refs(doc, "fixture.org"))}
    assert original.isdisjoint(assigned)


def test_assignment_does_not_mutate_the_original(doc: OrgDocument) -> None:
    before = doc.dump()
    assign_refs(doc, "fixture.org")
    assert doc.dump() == before


def test_only_refs_change(doc: OrgDocument) -> None:
    """Ref assignment must not disturb any other part of the document.

    content_hash is exactly "the document with its refs ignored", so it
    answers this without a second ref-stripper to keep in step.
    """
    assert content_hash(assign_refs(doc, "fixture.org")) == content_hash(doc)


def test_nodes_inside_properties_are_reached() -> None:
    """A headline's title is a secondary string and may hold nodes."""
    inner = {
        "$$data_type": "org-node",
        "type": "bold",
        "contents": ["loud"],
    }
    doc = OrgDocument.model_validate(
        {
            "contents": [
                {
                    "$$data_type": "org-node",
                    "type": "headline",
                    "properties": {"title": ["a ", inner]},
                }
            ]
        }
    )
    assigned = assign_refs(doc, "a.org")
    headline = assigned.contents[0]
    emphasis = headline.properties.title[1]
    assert emphasis.ref is not None
    assert emphasis.ref != headline.ref


def test_property_key_order_does_not_matter() -> None:
    """Refs must not depend on the order the source engine emitted keys."""
    node = {
        "$$data_type": "org-node",
        "type": "headline",
        "properties": {
            "title": ["t"],
            "zzz-extra": 1,
            "aaa-extra": 2,
        },
    }
    shuffled = {
        "$$data_type": "org-node",
        "type": "headline",
        "properties": {
            "aaa-extra": 2,
            "title": ["t"],
            "zzz-extra": 1,
        },
    }
    first = assign_refs(OrgDocument.model_validate({"contents": [node]}), "a")
    second = assign_refs(
        OrgDocument.model_validate({"contents": [shuffled]}), "a"
    )
    assert first.contents[0].ref == second.contents[0].ref
