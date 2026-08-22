# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The org-file directory backend."""

from __future__ import annotations

from pathlib import Path

import pytest

from org_store.adapters.orgparser import parse_file
from org_store.backends import BACKENDS, OrgDirectoryStore, get, open_backend
from org_store.model import walk
from org_store.service import OrgStore
from org_store.store import DocumentNotFound, StoreBackend, StoreError

DATA = Path(__file__).parent / "data"


@pytest.fixture
def files(tmp_path) -> OrgDirectoryStore:
    with OrgDirectoryStore(tmp_path / "org") as store:
        yield store


@pytest.fixture
def filestore(files: OrgDirectoryStore) -> OrgStore:
    return OrgStore(files)


@pytest.fixture
def kept(filestore: OrgStore) -> str:
    return filestore.create(parse_file(DATA / "fixture.org"), path_hint="fixture.org")


# -- the contract -----------------------------------------------------------


def test_satisfies_the_protocol(files: OrgDirectoryStore) -> None:
    assert isinstance(files, StoreBackend)


def test_documents_are_files_on_disk(files: OrgDirectoryStore, kept: str) -> None:
    assert (files.root / "fixture.org").is_file()


def test_a_document_id_is_a_relative_path(filestore: OrgStore, kept: str) -> None:
    assert kept == "fixture.org"
    assert filestore.list() == ["fixture.org"]


def test_existing_files_are_found_without_being_added(tmp_path) -> None:
    """The files are the storage, so anything else may write them."""
    root = tmp_path / "org"
    root.mkdir()
    (root / "hand-written.org").write_text("#+title: By hand\n\nBody.\n")
    store = OrgStore(OrgDirectoryStore(root))
    assert store.list() == ["hand-written.org"]
    assert store.summarise("hand-written.org").title == "By hand"


def test_nested_directories_are_searched(tmp_path) -> None:
    root = tmp_path / "org"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "deep.org").write_text("#+title: Deep\n")
    assert list(OrgDirectoryStore(root).list_documents()) == ["sub/deep.org"]


def test_missing_document(files: OrgDirectoryStore) -> None:
    with pytest.raises(DocumentNotFound):
        files.read_document("nope.org")
    with pytest.raises(DocumentNotFound):
        files.delete_document("nope.org")


def test_ids_cannot_escape_the_root(files: OrgDirectoryStore) -> None:
    """A document id is a path, so it must not be usable as a way out."""
    with pytest.raises(StoreError, match="escapes"):
        files.read_document("../../etc/passwd")


def test_allocate_id_makes_a_safe_filename(files: OrgDirectoryStore) -> None:
    assert files.allocate_id("Some Note!.org") == "Some-Note.org"
    assert files.allocate_id("../../etc/passwd") == "passwd.org"
    assert files.allocate_id(None).endswith(".org")


def test_allocate_id_avoids_collisions(files: OrgDirectoryStore, kept: str) -> None:
    assert files.allocate_id("fixture.org") == "fixture-2.org"


def test_delete_removes_the_file(filestore: OrgStore, files, kept: str) -> None:
    filestore.delete(kept)
    assert not (files.root / "fixture.org").exists()
    assert filestore.list() == []


# -- content ----------------------------------------------------------------


def test_the_file_is_readable_org(files: OrgDirectoryStore, kept: str) -> None:
    text = (files.root / "fixture.org").read_text()
    assert text.startswith("#+title: Canonical model fixture")
    assert "* TODO [#A] A headline with tags :work:urgent:" in text


def test_nodes_are_addressable(filestore: OrgStore, kept: str) -> None:
    """Refs are recomputed on read, since org text cannot carry one."""
    headline = next(n for n in walk(filestore.read(kept)) if n.type == "headline")
    assert headline.ref is not None
    assert filestore.read_node(kept, headline.ref).properties.raw_value == (
        "A headline with tags"
    )


def test_refs_are_stable_across_reads(filestore: OrgStore, kept: str) -> None:
    first = [n.ref for n in walk(filestore.read(kept))]
    assert first == [n.ref for n in walk(filestore.read(kept))]


def test_editing_a_node_rewrites_the_file(
    filestore: OrgStore, files, kept: str
) -> None:
    headline = next(n for n in walk(filestore.read(kept)) if n.type == "headline")
    filestore.update_node(
        kept,
        headline.ref,
        {
            "$$data_type": "org-node",
            "type": "paragraph",
            "contents": ["replaced entirely"],
        },
    )
    text = (files.root / "fixture.org").read_text()
    assert "replaced entirely" in text
    assert "A headline with tags" not in text


def test_a_write_does_not_lose_content(filestore: OrgStore, kept: str) -> None:
    """Reading and writing back must leave the document alone."""
    before = filestore.hash(kept)
    filestore.replace(kept, filestore.read(kept))
    assert filestore.hash(kept) == before


def test_hash_agrees_with_the_sqlite_store(tmp_path) -> None:
    """Sync compares hashes across backends, so they must mean one thing."""
    from org_store.backends import SqliteStore

    doc = parse_file(DATA / "fixture.org")
    files = OrgStore(OrgDirectoryStore(tmp_path / "org"))
    rows = OrgStore(SqliteStore(tmp_path / "store.sqlite"))
    assert files.hash(files.create(doc, path_hint="a.org")) == rows.hash(
        rows.create(doc, path_hint="a.org")
    )


def test_search_is_refused_rather_than_faked(filestore: OrgStore, kept: str) -> None:
    """This backend keeps no index; saying so beats returning nothing."""
    from org_store.search import SearchQuery

    with pytest.raises(StoreError, match="no search index"):
        filestore.search(SearchQuery(text="headline"))


def test_reindex_is_refused_too(filestore: OrgStore) -> None:
    with pytest.raises(StoreError, match="no derived tables"):
        filestore.reindex()


# -- the registry -----------------------------------------------------------


def test_the_backends_are_registered() -> None:
    assert set(BACKENDS) == {"sqlite", "orgfs", "emacs"}


def test_only_the_emacs_backend_needs_emacs() -> None:
    assert not get("sqlite").needs_emacs
    assert not get("orgfs").needs_emacs
    assert get("emacs").needs_emacs


def test_each_backend_names_its_own_setting() -> None:
    """Backends do not take the same settings, so each declares its own."""
    assert get("sqlite").setting == "store_database"
    assert get("orgfs").setting == "store_directory"


def test_unknown_backend_lists_the_real_ones() -> None:
    with pytest.raises(StoreError, match="orgfs"):
        get("postgres")


def test_open_backend(tmp_path) -> None:
    assert isinstance(open_backend("orgfs", tmp_path / "x"), OrgDirectoryStore)
