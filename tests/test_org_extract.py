# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The rows derived from a document, and the hash that stands for it."""

from __future__ import annotations

import json

from org_store.extract import content_hash, index_document
from org_store.model import OrgDocument
from org_store.refs import assign_refs


def indexed(raw: dict, doc_id: str = "fixture.org"):
    doc = assign_refs(OrgDocument.model_validate(raw), doc_id)
    return index_document(doc_id, doc)


def test_every_node_yields_a_row(fixture_json: dict) -> None:
    index = indexed(fixture_json)
    assert len(index.nodes) == 54
    assert {row.doc_id for row in index.nodes} == {"fixture.org"}


def test_node_rows_carry_structure(fixture_json: dict) -> None:
    index = indexed(fixture_json)
    headline = next(row for row in index.nodes if row.type == "headline")
    assert headline.heading_level == 1
    assert headline.todo_keyword == "TODO"
    assert headline.priority == "A"
    assert json.loads(headline.path_json) == [1]
    assert headline.parent_ref is None

    child = next(row for row in index.nodes if row.parent_ref == headline.ref)
    assert child.type == "section"


def test_tags_are_the_inherited_set(fixture_json: dict) -> None:
    """Searching for a tag should find headlines that inherit it."""
    index = indexed(fixture_json)
    headline = next(row for row in index.nodes if row.type == "headline")
    tags = {row.tag for row in index.tags if row.ref == headline.ref}
    # :work:urgent: are local; :physics:dune: come from #+filetags.
    assert tags == {"work", "urgent", "physics", "dune"}


def test_properties_come_from_the_drawer(fixture_json: dict) -> None:
    index = indexed(fixture_json)
    pairs = {(row.key, row.value) for row in index.properties}
    assert ("CUSTOM_ID", "a-headline") in pairs
    assert ("EFFORT", "2:00") in pairs
    assert ("ID", "9f8b7c6d-1234-4321-abcd-0123456789ab") in pairs


def test_links_are_indexed_by_target(fixture_json: dict) -> None:
    index = indexed(fixture_json)
    (link,) = index.links
    assert link.target == "https://orgmode.org"
    assert link.link_type == "https"


def test_planning_timestamps(fixture_json: dict) -> None:
    index = indexed(fixture_json)
    by_kind = {row.kind: row for row in index.timestamps}
    assert by_kind["scheduled"].start == "2026-08-24"
    assert by_kind["deadline"].start == "2026-08-30"
    # A CLOSED stamp carries a time, so it resolves to a full datetime.
    assert by_kind["closed"].start == "2026-08-21T14:03:00"


def test_timestamp_nodes(timestamps_json: dict) -> None:
    """A timestamp node keeps start/end on the node, not in properties."""
    index = indexed(timestamps_json, "ts.org")
    ranges = {row.kind: (row.start, row.end) for row in index.timestamps}
    assert ranges["active-range"][0].startswith("2026-")
    assert ranges["inactive"] == ("2026-07-01", "2026-07-01")
    # The repeating headline's SCHEDULED is a planning value, not a node.
    assert ranges["scheduled"] == ("2026-09-01", "2026-09-01")


def test_hash_ignores_refs(fixture_json: dict) -> None:
    """The same content under two ids must hash the same.

    Sync compares hashes across stores that assign refs from their own
    document ids; a hash that included refs would report every document as
    changed.
    """
    doc = OrgDocument.model_validate(fixture_json)
    assert content_hash(assign_refs(doc, "a.org")) == content_hash(
        assign_refs(doc, "b.org")
    )


def test_hash_notices_content(fixture_json: dict) -> None:
    doc = OrgDocument.model_validate(fixture_json)
    changed = doc.model_copy(deep=True)
    changed.properties.title = ["Something else"]
    assert content_hash(doc) != content_hash(changed)


def test_hash_is_order_independent(fixture_json: dict) -> None:
    doc = OrgDocument.model_validate(fixture_json)
    reordered = OrgDocument.model_validate(
        json.loads(json.dumps(fixture_json, sort_keys=True))
    )
    assert content_hash(doc) == content_hash(reordered)


def test_nodes_without_refs_are_skipped(fixture_json: dict) -> None:
    """Rows are keyed by ref, so an unassigned document indexes to nothing."""
    doc = OrgDocument.model_validate(fixture_json)
    for spot_ref in doc.contents:
        if not isinstance(spot_ref, str):
            spot_ref.ref = None
    index = index_document("x", doc)
    assert all(row.ref is not None for row in index.nodes)
