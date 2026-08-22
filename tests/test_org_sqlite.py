# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The sqlite backend: storage, derived tables, and their upkeep."""

from __future__ import annotations

import pytest

from org_store.backends import SqliteStore
from org_store.model import OrgDocument
from org_store.refs import assign_refs
from org_store.store import DocumentExists, DocumentNotFound, StoreBackend


def test_backend_satisfies_the_protocol(backend: SqliteStore) -> None:
    assert isinstance(backend, StoreBackend)


def test_create_and_read_round_trip(backend: SqliteStore, doc) -> None:
    doc_id = backend.create_document(doc, path_hint="a.org")
    assert doc_id == "a.org"
    assert backend.read_document(doc_id).dump() == doc.dump()


def test_create_generates_an_id_without_a_hint(backend: SqliteStore, doc) -> None:
    doc_id = backend.create_document(doc)
    assert doc_id and doc_id in backend.list_documents()


def test_duplicate_ids_are_refused(backend: SqliteStore, doc) -> None:
    backend.create_document(doc, path_hint="a.org")
    with pytest.raises(DocumentExists):
        backend.create_document(doc, path_hint="a.org")


def test_missing_document_raises(backend: SqliteStore) -> None:
    with pytest.raises(DocumentNotFound):
        backend.read_document("nope")
    with pytest.raises(DocumentNotFound):
        backend.content_hash("nope")
    with pytest.raises(DocumentNotFound):
        backend.delete_document("nope")


def test_list_is_sorted(backend: SqliteStore, doc) -> None:
    for name in ("c.org", "a.org", "b.org"):
        backend.create_document(doc, path_hint=name)
    assert list(backend.list_documents()) == ["a.org", "b.org", "c.org"]


def test_same_content_under_several_ids(backend: SqliteStore, doc) -> None:
    """Index rows are keyed per document, not globally by ref.

    Storing a document whose refs were never reassigned -- as happens
    when a backend is driven directly rather than through the service --
    must not collide with another document's rows.
    """
    for name in ("a.org", "b.org"):
        backend.create_document(doc, path_hint=name)
    assert backend.counts("a.org") == backend.counts("b.org")
    assert backend.counts("a.org")["nodes"] == 54


def test_replace_updates_content_and_hash(backend: SqliteStore, doc) -> None:
    doc_id = backend.create_document(doc, path_hint="a.org")
    before = backend.content_hash(doc_id)

    changed = doc.model_copy(deep=True)
    changed.contents.pop()
    backend.replace_document(doc_id, changed)

    assert backend.content_hash(doc_id) != before
    assert len(backend.read_document(doc_id).contents) == len(doc.contents) - 1


def test_replace_refuses_an_unknown_document(backend: SqliteStore, doc) -> None:
    with pytest.raises(DocumentNotFound):
        backend.replace_document("nope", doc)


def test_derived_rows_are_written(backend: SqliteStore, doc) -> None:
    doc_id = backend.create_document(assign_refs(doc, "a.org"), path_hint="a.org")
    counts = backend.counts(doc_id)
    assert counts["nodes"] == 54
    assert counts["tags"] > 0
    assert counts["properties"] > 0
    assert counts["links"] == 1
    assert counts["timestamps"] == 3


def test_delete_takes_the_derived_rows_with_it(backend: SqliteStore, doc) -> None:
    doc_id = backend.create_document(assign_refs(doc, "a.org"), path_hint="a.org")
    backend.delete_document(doc_id)
    assert list(backend.list_documents()) == []
    for table in ("nodes", "tags", "properties", "links", "timestamps"):
        rows = backend._db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
        assert rows["n"] == 0, table


def test_replace_does_not_leave_stale_rows(backend: SqliteStore, doc) -> None:
    """Derived rows are rebuilt, not added to."""
    doc_id = backend.create_document(assign_refs(doc, "a.org"), path_hint="a.org")
    before = backend.counts(doc_id)
    backend.replace_document(doc_id, assign_refs(doc, "a.org"))
    assert backend.counts(doc_id) == before


def test_shrinking_a_document_shrinks_the_index(backend: SqliteStore, doc) -> None:
    doc_id = backend.create_document(assign_refs(doc, "a.org"), path_hint="a.org")
    empty = OrgDocument.model_validate({"contents": []})
    backend.replace_document(doc_id, empty)
    assert backend.counts(doc_id)["nodes"] == 0


def test_tag_query(backend: SqliteStore, doc) -> None:
    backend.create_document(assign_refs(doc, "a.org"), path_hint="a.org")
    found = backend.refs_with_tag("urgent")
    assert found and all(doc_id == "a.org" for doc_id, _ in found)


def test_property_query(backend: SqliteStore, doc) -> None:
    backend.create_document(assign_refs(doc, "a.org"), path_hint="a.org")
    assert backend.refs_with_property("CUSTOM_ID", "a-headline")
    assert backend.refs_with_property("CUSTOM_ID")
    assert backend.refs_with_property("CUSTOM_ID", "wrong") == []


def test_link_query(backend: SqliteStore, doc) -> None:
    backend.create_document(assign_refs(doc, "a.org"), path_hint="a.org")
    assert backend.links_to("https://orgmode.org")
    assert backend.links_to("https://example.invalid") == []


def test_reindex_rebuilds_from_the_documents_table(
    backend: SqliteStore, doc
) -> None:
    """Dropping every derived row loses nothing."""
    doc_id = backend.create_document(assign_refs(doc, "a.org"), path_hint="a.org")
    before = backend.counts(doc_id)

    backend._db.execute("DELETE FROM nodes")  # cascades to the rest
    backend._db.commit()
    assert backend.counts(doc_id)["nodes"] == 0

    assert backend.reindex_all() == 1
    assert backend.counts(doc_id) == before


def test_backends_do_not_address_nodes(backend: SqliteStore) -> None:
    """Node addressing is the service's job, not a backend's.

    A backend that offered update_node would either duplicate the tree
    walk or write a node and leave the document's refs stale.
    """
    for name in ("read_node", "update_node", "delete_node"):
        assert not hasattr(backend, name), name


def test_allocate_id_does_not_store(backend: SqliteStore) -> None:
    """The id has to be knowable before the write, since refs hash it in."""
    assert backend.allocate_id("a.org") == "a.org"
    assert backend.allocate_id() != backend.allocate_id()
    assert list(backend.list_documents()) == []


def test_a_file_backed_store_persists(tmp_path, doc) -> None:
    path = tmp_path / "nested" / "store.sqlite"
    with SqliteStore(path) as first:
        first.create_document(doc, path_hint="a.org")
    assert path.exists()
    with SqliteStore(path) as second:
        assert list(second.list_documents()) == ["a.org"]
