# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""A store backend over a directory of org files.

The files *are* the storage: there is no database and no derived state,
so anything else editing them -- Emacs, git, a text editor -- is a
first-class writer rather than something to be reconciled with.  Reading
goes through :mod:`org_store.adapters.orgparser`, writing through
:mod:`org_store.render`, and neither needs Emacs running.

A document id is a path relative to the root, so it is stable, readable,
and means the same thing to a person as to the store.  Ids never escape
the root: a caller cannot address ``../../etc/passwd`` by asking for it.

Writing re-renders the whole file from the canonical model.  That is
lossless for everything the model carries but not byte-preserving --
blank lines and table alignment come back normalised.  See
:meth:`OrgDirectoryStore.replace_document`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from ..adapters.orgparser import parse_file
from ..extract import content_hash
from ..model import OrgDocument
from ..render import render_document
from ..store import DocId, DocumentExists, DocumentNotFound, StoreError

#: What an org file is called.
SUFFIX = ".org"

#: Characters that have no business in a generated filename.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class OrgDirectoryStore:
    """Documents as org files under one directory."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        """Nothing is held open; here so every backend closes the same way."""

    def __enter__(self) -> OrgDirectoryStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- documents ----------------------------------------------------------

    def allocate_id(self, path_hint: str | None = None) -> DocId:
        """Turn a hint into a free filename under the root.

        A hint that names an existing document is returned unchanged --
        it is a request for *that* document, and refusing it is
        :meth:`create_document`'s job, not this one's.
        """
        stem = _safe_stem(path_hint) if path_hint else "note"
        candidate = f"{stem}{SUFFIX}"
        if path_hint and not self._path(candidate).exists():
            return candidate
        number = 2
        while self._path(candidate).exists():
            candidate = f"{stem}-{number}{SUFFIX}"
            number += 1
        return candidate

    def create_document(
        self, doc: OrgDocument, *, path_hint: str | None = None
    ) -> DocId:
        doc_id = self.allocate_id(path_hint)
        path = self._path(doc_id)
        if path.exists():
            raise DocumentExists(doc_id)
        self._write(path, doc)
        return doc_id

    def read_document(self, doc_id: DocId) -> OrgDocument:
        """Read one file, in a child process.

        The files here are whatever is in the directory, and the parser
        can die on one rather than raise -- see
        :func:`org_store.adapters.orgparser.parse_file`.  A store that
        took the program down over one unreadable file would be worse
        than one that costs a fork per read.
        """
        return parse_file(self._existing(doc_id))

    def replace_document(self, doc_id: DocId, doc: OrgDocument) -> None:
        """Rewrite a document's file from the canonical model.

        The whole file is re-rendered rather than patched in place.  That
        keeps the file and the model in step, but it does normalise
        formatting the model does not record -- run-in blank lines, table
        column alignment -- so a file round-tripped through here comes
        back tidy rather than untouched.
        """
        self._write(self._existing(doc_id), doc)

    def delete_document(self, doc_id: DocId) -> None:
        self._existing(doc_id).unlink()

    def list_documents(self) -> Iterable[DocId]:
        return sorted(
            str(path.relative_to(self.root))
            for path in self.root.rglob(f"*{SUFFIX}")
            if path.is_file()
        )

    def content_hash(self, doc_id: DocId) -> str:
        """A digest of the document's content, not of its file.

        The bytes on disk would be far cheaper, but sync compares hashes
        taken from two different backends and a file digest could never
        equal one taken from sqlite -- every document would read as
        changed.  So this means the same thing everywhere, at the cost of
        parsing the file to find out.
        """
        return content_hash(self.read_document(doc_id))

    def render(self, doc: OrgDocument) -> str:
        """The org text this backend would write for DOC."""
        return render_document(doc)

    # -- internals ----------------------------------------------------------

    def _path(self, doc_id: DocId) -> Path:
        """The file a document id names, refusing anything outside the root."""
        candidate = (self.root / doc_id).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise StoreError(f"document id escapes the store root: {doc_id}")
        return candidate

    def _existing(self, doc_id: DocId) -> Path:
        path = self._path(doc_id)
        if not path.is_file():
            raise DocumentNotFound(doc_id)
        return path

    def _write(self, path: Path, doc: OrgDocument) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = render_document(doc)
        if text and not text.endswith("\n"):
            text += "\n"
        path.write_text(text, encoding="utf-8")


def _safe_stem(hint: str) -> str:
    """A filename stem from a hint, without directories or surprises."""
    stem = Path(hint).name
    if stem.endswith(SUFFIX):
        stem = stem[: -len(SUFFIX)]
    cleaned = _UNSAFE.sub("-", stem).strip("-.")
    return cleaned or "note"
