# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Copying documents between stores, and the hash that makes it possible."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from org_store.adapters.orgparser import parse_file
from org_store.backends import OrgDirectoryStore, SqliteStore
from org_store.extract import content_hash
from org_store.model import OrgDocument, walk
from org_store.render import render_document
from org_store.service import OrgStore
from org_store.sync import Outcome, sync

DATA = Path(__file__).parent / "data"

#: Fixtures with both an org form and a real ox-json export.
PAIRS = sorted(
    path.stem.removesuffix(".ox-json")
    for path in DATA.glob("*.ox-json.json")
    if (DATA / f"{path.stem.removesuffix('.ox-json')}.org").exists()
)


@pytest.fixture
def rows(tmp_path) -> OrgStore:
    return OrgStore(SqliteStore(tmp_path / "store.sqlite"))


@pytest.fixture
def files(tmp_path) -> OrgStore:
    return OrgStore(OrgDirectoryStore(tmp_path / "org"))


@pytest.fixture
def stocked(rows: OrgStore) -> OrgStore:
    for name in ("fixture.org", "fixture-timestamps.org"):
        rows.create(parse_file(DATA / name), path_hint=name)
    return rows


# -- the portable hash ------------------------------------------------------


@pytest.mark.parametrize("name", PAIRS)
def test_the_hash_is_the_same_whichever_engine_read_it(name: str) -> None:
    """The whole basis of syncing between differently-read stores.

    ox-json carries org-element bookkeeping the standalone walker cannot
    reproduce, so the two never agree on JSON.  They do agree on the org
    text, which is what the document actually says, so that is what is
    hashed.
    """
    emacs = OrgDocument.model_validate(
        json.loads((DATA / f"{name}.ox-json.json").read_text())
    )
    walker = parse_file(DATA / f"{name}.org")
    assert emacs.dump() != walker.dump()
    assert content_hash(emacs) == content_hash(walker)


def test_the_hash_is_of_the_rendered_text() -> None:
    import hashlib

    doc = parse_file(DATA / "fixture.org")
    expected = hashlib.sha256(render_document(doc).encode()).hexdigest()
    assert content_hash(doc) == expected


def test_the_hash_ignores_the_document_id() -> None:
    """The same content under two ids must be recognised as the same."""
    doc = parse_file(DATA / "fixture.org")
    from org_store.refs import assign_refs

    assert content_hash(assign_refs(doc, "a.org")) == content_hash(
        assign_refs(doc, "b.org")
    )


def test_the_hash_ignores_formatting_the_model_does_not_record(tmp_path) -> None:
    """A hand-aligned table and a tidy one say the same thing."""
    loose = tmp_path / "loose.org"
    tight = tmp_path / "tight.org"
    loose.write_text("| a  |   b |\n|----+-----|\n| 1  |   2 |\n")
    tight.write_text("| a | b |\n|---|\n| 1 | 2 |\n")
    assert content_hash(parse_file(loose)) == content_hash(parse_file(tight))


def test_the_hash_notices_a_real_change(tmp_path) -> None:
    one = tmp_path / "one.org"
    one.write_text("* Heading\ntext\n")
    before = content_hash(parse_file(one))
    one.write_text("* Heading\ndifferent text\n")
    assert content_hash(parse_file(one)) != before


def test_backends_agree_on_a_document_they_both_hold(
    stocked: OrgStore, files: OrgStore
) -> None:
    sync(stocked, files)
    for doc_id in stocked.list():
        assert stocked.hash(doc_id) == files.hash(doc_id)


# -- copying ----------------------------------------------------------------


def test_new_documents_are_created(stocked: OrgStore, files: OrgStore) -> None:
    report = sync(stocked, files)
    assert {c.outcome for c in report.changes} == {Outcome.CREATED}
    assert files.list() == stocked.list()


def test_a_second_run_does_nothing(stocked: OrgStore, files: OrgStore) -> None:
    """Idempotent, which is what a portable hash is for."""
    sync(stocked, files)
    report = sync(stocked, files)
    assert {c.outcome for c in report.changes} == {Outcome.UNCHANGED}
    assert report.written == 0


def test_a_changed_document_is_updated(
    stocked: OrgStore, files: OrgStore, tmp_path
) -> None:
    sync(stocked, files)
    path = tmp_path / "org" / "fixture.org"
    path.write_text(path.read_text() + "\n* Added at the destination\n")

    report = sync(stocked, files)
    assert [c.outcome for c in report.changes if c.doc_id == "fixture.org"] == [
        Outcome.UPDATED
    ]
    assert "Added at the destination" not in path.read_text()


def test_the_source_always_wins(stocked: OrgStore, files: OrgStore) -> None:
    """A copy, not a merge: an edit at the destination is overwritten.

    Comparing hashes says the two differ, not which side moved, so there
    is nothing here that could preserve the destination's version.
    """
    sync(stocked, files)
    doc = files.read("fixture.org")
    doc.contents.clear()
    files.replace("fixture.org", doc)

    sync(stocked, files)
    assert files.hash("fixture.org") == stocked.hash("fixture.org")


def test_content_survives_the_crossing(stocked: OrgStore, files: OrgStore) -> None:
    sync(stocked, files)
    before = [n.type for n in walk(stocked.read("fixture.org"))]
    assert [n.type for n in walk(files.read("fixture.org"))] == before


def test_the_destination_indexes_what_it_receives(
    files: OrgStore, rows: OrgStore
) -> None:
    """Sync goes through the service, so the destination indexes normally."""
    from org_store.search import SearchQuery

    files.create(parse_file(DATA / "fixture.org"), path_hint="fixture.org")
    sync(files, rows)
    assert rows.search(SearchQuery(text="hello"))


def test_a_subset_can_be_named(stocked: OrgStore, files: OrgStore) -> None:
    report = sync(stocked, files, doc_ids=["fixture.org"])
    assert [c.doc_id for c in report.changes] == ["fixture.org"]
    assert files.list() == ["fixture.org"]


# -- dry runs and pruning ---------------------------------------------------


def test_a_dry_run_writes_nothing(stocked: OrgStore, files: OrgStore) -> None:
    report = sync(stocked, files, dry_run=True)
    assert report.dry_run
    assert {c.outcome for c in report.changes} == {Outcome.CREATED}
    assert files.list() == []


def test_pruning_is_off_by_default(stocked: OrgStore, files: OrgStore) -> None:
    files.create(parse_file(DATA / "fixture.org"), path_hint="only-here.org")
    sync(stocked, files)
    assert "only-here.org" in files.list()


def test_pruning_removes_what_the_source_lacks(
    stocked: OrgStore, files: OrgStore
) -> None:
    files.create(parse_file(DATA / "fixture.org"), path_hint="only-here.org")
    report = sync(stocked, files, prune=True)
    assert Outcome.DELETED in {c.outcome for c in report.changes}
    assert "only-here.org" not in files.list()


def test_pruning_is_ignored_for_a_partial_run(
    stocked: OrgStore, files: OrgStore
) -> None:
    """With a subset named, "absent" cannot mean "deleted"."""
    files.create(parse_file(DATA / "fixture.org"), path_hint="only-here.org")
    sync(stocked, files, doc_ids=["fixture.org"], prune=True)
    assert "only-here.org" in files.list()


# -- failure ----------------------------------------------------------------


def test_one_bad_document_does_not_strand_the_rest(
    stocked: OrgStore, files: OrgStore, tmp_path
) -> None:
    """A run must not stop half way through and leave no report."""

    class Awkward:
        def __init__(self, real):
            self.real = real

        def __getattr__(self, name):
            return getattr(self.real, name)

        def read(self, doc_id):
            if doc_id == "fixture.org":
                raise RuntimeError("cannot read this one")
            return self.real.read(doc_id)

    report = sync(Awkward(stocked), files)
    assert report.count(Outcome.FAILED) == 1
    assert report.count(Outcome.CREATED) == 1
    assert report.failed[0].doc_id == "fixture.org"
    assert files.list() == ["fixture-timestamps.org"]


def test_round_trip_between_backends(stocked: OrgStore, files: OrgStore) -> None:
    """Out to files and back again must change nothing."""
    before = {doc_id: stocked.hash(doc_id) for doc_id in stocked.list()}
    sync(stocked, files)
    sync(files, stocked)
    assert {doc_id: stocked.hash(doc_id) for doc_id in stocked.list()} == before
