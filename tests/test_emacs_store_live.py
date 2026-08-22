# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The Emacs-backed store, against a real daemon.

These need an Emacs with ``ox-json`` installed, which is not something
this project can install for you -- so they skip rather than fail when it
is absent, the way the rest of the ``emacs``-marked suite treats a
missing Emacs.

The conformance comparison that runs everywhere lives in
``test_org_conformance.py``; this checks the parts only a live daemon can
exercise: the export round trip, and the buffer safety that keeps the
store from overwriting someone's unsaved work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from org_cli_helper.config import Config
from org_cli_helper.emacs import DaemonError, Emacs
from org_store.backends import EmacsDirectoryStore, OrgExporter
from org_store.extract import content_hash
from org_store.model import OrgDocument, walk
from org_store.service import OrgStore
from org_store.store import StoreError

pytestmark = pytest.mark.emacs

DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def emacs() -> Emacs:
    """A daemon that can export JSON, or a skip saying why not."""
    client = Emacs(Config(server_name="org-cli-helper-store-test", lifetime=120))
    try:
        client.eval("(emacs-version)")
    except DaemonError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no usable Emacs daemon: {exc}")

    probe = DATA / "fixture-timestamps.org"
    try:
        client.export_json(probe)
    except DaemonError as exc:  # pragma: no cover - environment dependent
        client.stop()
        pytest.skip(f"Emacs cannot export JSON (is ox-json installed?): {exc}")
    yield client
    client.stop()


@pytest.fixture
def store(emacs: Emacs, tmp_path) -> OrgStore:
    root = tmp_path / "org"
    root.mkdir()
    (root / "fixture.org").write_text((DATA / "fixture.org").read_text())
    return OrgStore(EmacsDirectoryStore(root, emacs))


def test_emacs_satisfies_the_exporter_port(emacs: Emacs) -> None:
    """org_store declares what it needs; Emacs already does it."""
    assert isinstance(emacs, OrgExporter)


def test_export_returns_a_canonical_document(emacs: Emacs) -> None:
    payload = emacs.export_json(DATA / "fixture.org")
    doc = OrgDocument.model_validate(payload)
    assert doc.properties.title == ["Canonical model fixture"]
    assert len(list(walk(doc))) > 40


def test_export_agrees_with_the_committed_fixture(emacs: Emacs) -> None:
    """The checked-in oracle must still describe the same document.

    Not byte-for-byte: the fixture was captured under ``emacs -Q`` with
    the bundled Org, and a real init file brings its own Org version and
    babel defaults.  Those move ``creator``, ``post-blank`` and a src
    block's header arguments without changing what the document says.
    So this compares what the conformance suite compares -- structure and
    text -- which is what the fixture is relied on for.
    """
    import json

    live = OrgDocument.model_validate(emacs.export_json(DATA / "fixture.org"))
    committed = OrgDocument.model_validate(
        json.loads((DATA / "fixture.ox-json.json").read_text())
    )

    def structure(doc):
        return [
            (
                node.type,
                node.properties.get("raw_value"),
                node.properties.get("level"),
                list(node.properties.get("tags") or []),
            )
            for node in walk(doc)
        ]

    from org_store.text import plain_text

    assert structure(live) == structure(committed)
    assert plain_text(live.contents).split() == plain_text(committed.contents).split()


def test_reading_through_the_store(store: OrgStore) -> None:
    summary = store.summarise("fixture.org")
    assert summary.title == "Canonical model fixture"
    assert summary.headings == 3


def test_nodes_are_addressable(store: OrgStore) -> None:
    headline = next(
        n for n in walk(store.read("fixture.org")) if n.type == "headline"
    )
    assert store.read_node("fixture.org", headline.ref).properties.raw_value == (
        "A headline with tags"
    )


def test_the_two_engines_hash_differently(store: OrgStore) -> None:
    """A content hash is only comparable between stores reading alike.

    ox-json carries org-element's own bookkeeping -- ``mode``,
    ``true-level``, ``post-blank`` on everything -- which the standalone
    walker has no way to reproduce and no reason to.  The documents mean
    the same thing, as the conformance suite asserts, but they are not
    the same JSON, so their hashes differ.

    Recorded rather than worked around, because sync compares hashes
    across backends: two stores of the same files, one read through
    Emacs and one not, would see every document as changed.  Deciding
    what sync should compare instead is a Phase 6 question.
    """
    from org_store.backends import OrgDirectoryStore

    plain = OrgStore(OrgDirectoryStore(store.backend.root))
    assert store.hash("fixture.org") != plain.hash("fixture.org")

    # What does agree is the text, which is what "the same document" means.
    from org_store.text import plain_text

    assert plain_text(store.read("fixture.org").contents).split() == plain_text(
        plain.read("fixture.org").contents
    ).split()


def test_writing_reverts_the_emacs_buffer(store: OrgStore, emacs: Emacs) -> None:
    """Emacs must not go on showing content the file no longer has."""
    path = store.backend.root / "fixture.org"
    emacs.eval(f'(find-file-noselect "{path}")')
    assert emacs.buffer_state(path)["visiting"] is True

    # Change actual content: the document's title lives in a keyword node
    # in its first section, which is what the renderer writes.
    doc = store.read("fixture.org")
    doc.contents.append(
        OrgDocument.model_validate(
            {
                "contents": [
                    {
                        "$$data_type": "org-node",
                        "type": "headline",
                        "properties": {"level": 1, "raw-value": "Written from the store"},
                    }
                ]
            }
        ).contents[0]
    )
    store.replace("fixture.org", doc)

    assert "Written from the store" in path.read_text()
    # Emacs was reverted, so it is not still showing the old file.
    titles = [
        node.properties.get("raw_value")
        for node in walk(OrgDocument.model_validate(emacs.export_json(path)))
        if node.type == "headline"
    ]
    assert "Written from the store" in titles


def test_unsaved_changes_are_not_overwritten(store: OrgStore, emacs: Emacs) -> None:
    """The store is one writer among several, and must not win by default.

    Overwriting a modified buffer would destroy work that exists only in
    Emacs, so it is refused rather than merged.
    """
    path = store.backend.root / "fixture.org"
    emacs.eval(
        f'(with-current-buffer (find-file-noselect "{path}")'
        ' (goto-char (point-max)) (insert "* Unsaved work\\n"))'
    )
    assert emacs.buffer_state(path)["modified"] is True

    with pytest.raises(StoreError, match="unsaved changes"):
        store.replace("fixture.org", store.read("fixture.org"))
    with pytest.raises(StoreError, match="unsaved changes"):
        store.delete("fixture.org")

    # The file is untouched, and Emacs still has the work.
    assert "Unsaved work" not in path.read_text()
    emacs.eval(
        f'(with-current-buffer (find-file-noselect "{path}")'
        " (set-buffer-modified-p nil))"
    )


def test_export_sees_unsaved_buffer_content(store: OrgStore, emacs: Emacs) -> None:
    """Reading through Emacs means reading what Emacs has, not the file.

    That is the one thing this backend can do that no reader of the file
    can, and the reason it exists.
    """
    path = store.backend.root / "fixture.org"
    emacs.eval(
        f'(with-current-buffer (find-file-noselect "{path}")'
        ' (goto-char (point-max)) (insert "* Only in the buffer\\n"))'
    )
    try:
        titles = [
            node.properties.get("raw_value")
            for node in walk(store.read("fixture.org"))
            if node.type == "headline"
        ]
        assert "Only in the buffer" in titles
        assert "Only in the buffer" not in path.read_text()
    finally:
        emacs.eval(
            f'(with-current-buffer (find-file-noselect "{path}")'
            " (set-buffer-modified-p nil))"
        )
