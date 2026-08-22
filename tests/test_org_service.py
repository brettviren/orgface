# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""CRUDS semantics: what the service layer guarantees on top of a backend."""

from __future__ import annotations

import pytest

from org_store.model import OrgDocument, walk
from org_store.refs import compute_ref
from org_store.service import OrgStore
from org_store.text import node_text, plain_text
from org_store.store import DocumentNotFound, NodeNotFound


def test_create_assigns_our_own_refs(store: OrgStore, fixture_json: dict) -> None:
    """Whatever the exporter called each node is replaced on the way in."""
    exported = {node.ref for node in walk(OrgDocument.model_validate(fixture_json))}
    doc_id = store.create(fixture_json, path_hint="fixture.org")
    stored_refs = {node.ref for node in walk(store.read(doc_id))}
    assert stored_refs.isdisjoint(exported)
    assert None not in stored_refs


def test_refs_match_the_documented_algorithm(
    store: OrgStore, fixture_json: dict
) -> None:
    doc_id = store.create(fixture_json, path_hint="fixture.org")
    first = store.read(doc_id).contents[0]
    assert first.ref == compute_ref(doc_id, (0,))


def test_create_accepts_a_model_too(store: OrgStore, doc: OrgDocument) -> None:
    doc_id = store.create(doc, path_hint="a.org")
    assert store.read(doc_id).properties.title == doc.properties.title


def test_create_validates(store: OrgStore) -> None:
    """A bad document fails as a StoreError, not as a pydantic error.

    Adapters catch StoreError; none of them should need pydantic.
    """
    from org_store.store import InvalidDocument

    with pytest.raises(InvalidDocument) as caught:
        store.create({"contents": [{"$$data_type": "org-node"}]})  # no type
    assert caught.value.errors > 0


def test_read_write_delete(store: OrgStore, fixture_json: dict) -> None:
    doc_id = store.create(fixture_json, path_hint="a.org")
    assert doc_id in store.list()
    store.delete(doc_id)
    assert doc_id not in store.list()
    with pytest.raises(DocumentNotFound):
        store.read(doc_id)


def test_replace_keeps_the_id_and_reassigns_refs(
    store: OrgStore, stored: str
) -> None:
    replacement = OrgDocument.model_validate(
        {
            "properties": {"title": ["Replaced"]},
            "contents": [
                {"$$data_type": "org-node", "type": "paragraph", "contents": ["x"]}
            ],
        }
    )
    store.replace(stored, replacement)
    read = store.read(stored)
    assert read.properties.title == ["Replaced"]
    assert read.contents[0].ref == compute_ref(stored, (0,))


def test_hash_is_stable_across_a_rewrite(store: OrgStore, stored: str) -> None:
    before = store.hash(stored)
    store.replace(stored, store.read(stored))
    assert store.hash(stored) == before


def test_read_node(store: OrgStore, stored: str) -> None:
    headline = next(n for n in walk(store.read(stored)) if n.type == "headline")
    assert store.read_node(stored, headline.ref).ref == headline.ref


def test_read_unknown_node(store: OrgStore, stored: str) -> None:
    with pytest.raises(NodeNotFound):
        store.read_node(stored, "orgmissing")


def test_update_node_returns_the_ref_it_now_has(
    store: OrgStore, stored: str
) -> None:
    """Refs name positions, so an edit can rename what it just wrote."""
    target = store.read(stored).contents[0]
    new_ref = store.update_node(
        stored,
        target.ref,
        {"$$data_type": "org-node", "type": "quote-block", "contents": ["new"]},
    )
    written = store.read_node(stored, new_ref)
    assert written.type == "quote-block"
    assert written.contents == ["new"]


def test_update_node_accepts_a_raw_dict_of_any_type(
    store: OrgStore, stored: str
) -> None:
    """A dict is routed through the union, not stored as a generic blob."""
    from org_store.model import SrcBlockNode

    target = store.read(stored).contents[0]
    ref = store.update_node(
        stored,
        target.ref,
        {
            "$$data_type": "org-node",
            "type": "src-block",
            "properties": {"language": "sh", "value": "echo hi\n"},
        },
    )
    written = store.read_node(stored, ref)
    assert isinstance(written, SrcBlockNode)
    assert written.properties.language == "sh"


def test_update_unknown_node(store: OrgStore, stored: str) -> None:
    with pytest.raises(NodeNotFound):
        store.update_node(stored, "orgmissing", {"type": "paragraph"})


def test_delete_node_removes_it(store: OrgStore, stored: str) -> None:
    before = len(list(walk(store.read(stored))))
    doomed = store.read(stored).contents[0]
    store.delete_node(stored, doomed.ref)
    assert len(list(walk(store.read(stored)))) < before


def test_delete_node_reassigns_later_refs(store: OrgStore, stored: str) -> None:
    """Removing a node shifts its siblings, so their refs must change."""
    contents = store.read(stored).contents
    later_ref_before = contents[1].ref
    store.delete_node(stored, contents[0].ref)
    assert store.read(stored).contents[0].ref != later_ref_before
    assert store.read(stored).contents[0].ref == compute_ref(stored, (0,))


def test_delete_unknown_node(store: OrgStore, stored: str) -> None:
    with pytest.raises(NodeNotFound):
        store.delete_node(stored, "orgmissing")


def test_index_is_recomputed_not_read_back(store: OrgStore, stored: str) -> None:
    index = store.index(stored)
    assert len(index.nodes) == 54
    assert {row.doc_id for row in index.nodes} == {stored}


def test_summarise(store: OrgStore, stored: str) -> None:
    summary = store.summarise(stored)
    assert summary.doc_id == stored
    assert summary.title == "Canonical model fixture"
    assert summary.filetags == ["physics", "dune"]
    assert summary.headings == 3
    assert summary.links == 1
    assert "urgent" in summary.tags
    assert summary.content_hash == store.hash(stored)


def test_briefs_skip_the_expensive_fields(store: OrgStore, stored: str) -> None:
    """Listing must not walk every document just to print a title."""
    (brief,) = store.briefs()
    assert brief.doc_id == stored
    assert brief.title == "Canonical model fixture"
    assert brief.nodes == 0  # not computed for a listing


def test_summarise_node(store: OrgStore, stored: str) -> None:
    headline = next(n for n in walk(store.read(stored)) if n.type == "headline")
    summary = store.summarise_node(stored, headline.ref)
    assert summary.ref == headline.ref
    assert summary.type == "headline"
    assert "Body text under the headline." in summary.text


def test_plain_text_keeps_verbatim_values(store: OrgStore, stored: str) -> None:
    """Text held in properties.value is text too, and must not be lost."""
    doc = store.read(stored)
    fragment = next(n for n in walk(doc) if n.type == "latex-fragment")
    assert node_text(fragment) == "$E = mc^2$"

    block = next(n for n in walk(doc) if n.type == "src-block")
    assert node_text(block) == 'print("hello")\n'


def test_table_cells_do_not_run_together(store: OrgStore, stored: str) -> None:
    doc = store.read(stored)
    rows = [n for n in walk(doc) if n.type == "table-row"]
    assert node_text(rows[0]) == "name value"

    table = next(n for n in walk(doc) if n.type == "table")
    assert node_text(table) == "name value a 1 b 2"


def test_words_are_not_fused_across_nodes(store: OrgStore, stored: str) -> None:
    """org-element hides inter-node whitespace in post-blank."""
    doc = store.read(stored)
    paragraph = next(
        n
        for n in walk(doc)
        if n.type == "paragraph" and "footnote reference" in node_text(n)
    )
    text = node_text(paragraph)
    assert "referencein" not in text
    assert "A footnote reference in a paragraph." in text


def test_plain_text_flattens_markup() -> None:
    doc = OrgDocument.model_validate(
        {
            "contents": [
                {
                    "$$data_type": "org-node",
                    "type": "paragraph",
                    "contents": [
                        "a ",
                        {
                            "$$data_type": "org-node",
                            "type": "bold",
                            "contents": ["loud"],
                        },
                        " end",
                    ],
                }
            ]
        }
    )
    assert plain_text(doc.contents) == "a loud end"
