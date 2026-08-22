# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The interface every store backend implements.

A backend knows how to keep documents somewhere -- sqlite, a directory of
org files, an Emacs session -- and nothing else.  It does not validate,
does not decide what a ref means, and does not search.  That all belongs
to :mod:`org_store.service`, which is the only caller a backend should
expect.

Search is deliberately absent from this Protocol.  Not every backend can
answer a full-text or vector query at all, let alone efficiently, so
making it part of the contract would force every backend to either
reimplement or fake it.  It lives in the service layer instead, over
whichever indexes are available.

Node-level reads and writes are absent for a sharper reason.  Addressing
a node means walking the canonical tree, which no backend needs to know
how to do, and *writing* one means reassigning the refs of everything
after it -- a rule the service owns.  A backend that offered
``update_node`` would either duplicate that walk or, worse, write a node
and leave the document's refs stale.  Both belong above, expressed once
over ``read_document``/``replace_document``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from .model import OrgDocument

#: How a backend names a document.  Opaque to everything above: the sqlite
#: backend uses whatever it was given, a file backend a path relative to
#: its root.  Only the backend that issued one may interpret it.
DocId = str


class StoreError(Exception):
    """A store operation could not be carried out."""


class DocumentNotFound(StoreError):
    """No document in this store has that id."""

    def __init__(self, doc_id: DocId) -> None:
        super().__init__(f"no such document: {doc_id}")
        self.doc_id = doc_id


class NodeNotFound(StoreError):
    """No node in that document has that ref."""

    def __init__(self, doc_id: DocId, ref: str) -> None:
        super().__init__(f"no node {ref} in document {doc_id}")
        self.doc_id = doc_id
        self.ref = ref


class DocumentExists(StoreError):
    """A document already has the requested id."""

    def __init__(self, doc_id: DocId) -> None:
        super().__init__(f"document already exists: {doc_id}")
        self.doc_id = doc_id


class InvalidDocument(StoreError):
    """Content offered to the store is not a valid org document.

    Validation is the service layer's job, so the pydantic error it
    catches is re-raised as one of these: every adapter already handles
    :class:`StoreError`, and none of them should have to import pydantic
    to report a bad document.
    """

    def __init__(self, message: str, errors: int = 0) -> None:
        super().__init__(message)
        self.errors = errors


@runtime_checkable
class StoreBackend(Protocol):
    """Storage for whole documents and for nodes within them."""

    def allocate_id(self, path_hint: str | None = None) -> DocId:
        """Choose the id a document would be stored under, without storing.

        Separate from :meth:`create_document` because refs are a function
        of the document id, so the service has to know the id *before* it
        can assign refs and write once.  Folding the two together would
        force a create to store the document twice -- and leave it briefly
        persisted under refs from whichever engine produced it.

        ``path_hint`` is a request, not an instruction: a backend may use
        it as the id, as a filename, or ignore it entirely.
        """
        ...

    def create_document(
        self, doc: OrgDocument, *, path_hint: str | None = None
    ) -> DocId:
        """Store DOC as a new document and return the id it was given."""
        ...

    def read_document(self, doc_id: DocId) -> OrgDocument: ...

    def replace_document(self, doc_id: DocId, doc: OrgDocument) -> None: ...

    def delete_document(self, doc_id: DocId) -> None: ...

    def list_documents(self) -> Iterable[DocId]: ...

    def content_hash(self, doc_id: DocId) -> str:
        """A digest of the document's content, for sync change detection.

        Every backend must compute this the same way -- see
        :func:`org_store.extract.content_hash` -- or hashes taken from two
        backends cannot be compared, which is the only reason it exists.
        """
        ...
