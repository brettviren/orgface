# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""CRUDS over a store backend.

This is the only layer that knows what a create or an update *means*:
which refs a document ends up with, what has to be re-derived after a
write, and what a caller may address.  Backends below it only keep
documents; adapters above it -- the CLI, and later MCP and FastAPI -- only
translate their own argument conventions into calls on this class.

Anything a second adapter would otherwise have to reimplement belongs
here, so the views this returns are already-computed facts rather than
models an adapter would have to walk.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .embed import Embedder, EmbeddingError
from .extract import Index, index_document
from .model import OrgDocument, OrgNodeBase
from .refs import assign_refs, compute_ref
from .search import SearchQuery, SearchResult
from .store import DocId, InvalidDocument, NodeNotFound, StoreBackend, StoreError
from .text import document_title, node_text
from .tree import find

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocumentSummary:
    """Facts about one stored document, for an adapter to render."""

    doc_id: DocId
    title: str
    filetags: list[str] = field(default_factory=list)
    schema_version: int = 0
    content_hash: str = ""
    nodes: int = 0
    headings: int = 0
    tags: list[str] = field(default_factory=list)
    links: int = 0
    timestamps: int = 0
    #: Why this document could not be read, when it could not be.  A
    #: listing says so and carries on rather than failing entirely: one
    #: unreadable file among a thousand is a fact about that file.
    error: str = ""


@dataclass(frozen=True)
class NodeSummary:
    """Facts about one node, for an adapter to render."""

    doc_id: DocId
    ref: str
    type: str
    text: str


class OrgStore:
    """CRUDS operations against one backend.

    ``embedders`` are the embedding services semantic work may use, in
    the order to try them.  Several may share an identity -- the same
    model behind different services -- and the first that answers is
    used, so a store can be embedded and searched from wherever it is.
    """

    def __init__(
        self, backend: StoreBackend, embedders: Sequence[Embedder] = ()
    ) -> None:
        self.backend = backend
        self.embedders = list(embedders)

    # -- documents ----------------------------------------------------------

    def create(
        self, doc: OrgDocument | dict[str, Any], *, path_hint: str | None = None
    ) -> DocId:
        """Store a new document and return its id.

        The document is validated and its refs are assigned here, so a
        caller can hand over raw JSON from any source and get back
        something addressable.  The id is settled before the write, since
        refs are a function of it -- that way the document is written once
        and is never briefly stored under refs nobody can address.
        """
        validated = _coerce(doc)
        doc_id = self.backend.allocate_id(path_hint)
        return self.backend.create_document(
            assign_refs(validated, doc_id), path_hint=path_hint
        )

    def read(self, doc_id: DocId) -> OrgDocument:
        """Read a document, with its refs.

        Refs are assigned here rather than trusted from the backend.  They
        are a pure function of the document id and each node's structural
        path, so recomputing them is cheap and always right -- and it is
        the only thing that makes the org-file store addressable at all,
        since org text has nowhere to keep a ref.  A backend that does
        persist them, as sqlite does, is merely caching what this would
        have worked out anyway.
        """
        return assign_refs(self.backend.read_document(doc_id), doc_id, copy=False)

    def replace(self, doc_id: DocId, doc: OrgDocument | dict[str, Any]) -> None:
        """Replace a document's content, keeping its id."""
        self.backend.replace_document(doc_id, assign_refs(_coerce(doc), doc_id))

    def delete(self, doc_id: DocId) -> None:
        self.backend.delete_document(doc_id)

    def list(self) -> list[DocId]:
        return list(self.backend.list_documents())

    def hash(self, doc_id: DocId) -> str:
        """The content digest sync compares between backends."""
        return self.backend.content_hash(doc_id)

    # -- nodes --------------------------------------------------------------
    #
    # Addressing a node is a walk of the canonical tree, and writing one
    # means reassigning refs afterwards.  Neither is a backend's concern,
    # so both are expressed once here over read/replace_document.

    def read_node(self, doc_id: DocId, ref: str) -> OrgNodeBase:
        return self._locate(doc_id, ref)[1].node

    def update_node(
        self, doc_id: DocId, ref: str, node: OrgNodeBase | dict[str, Any]
    ) -> str:
        """Replace one node and return the ref it has afterwards.

        Refs name positions, so replacing a node with one of a different
        shape changes the refs of everything after it.  Refs are therefore
        reassigned across the whole document after the edit, and the new
        ref of the node just written is returned -- the ref passed in may
        no longer name it.
        """
        doc, spot = self._locate(doc_id, ref)
        spot.replace(_coerce_node(node))
        self._write_back(doc_id, doc)
        # assign_refs makes this true by construction; there is nothing to
        # look up.
        return compute_ref(doc_id, spot.path)

    def delete_node(self, doc_id: DocId, ref: str) -> None:
        """Remove one node, then reassign refs across the document."""
        doc, spot = self._locate(doc_id, ref)
        spot.remove()
        self._write_back(doc_id, doc)

    # -- derived views ------------------------------------------------------

    def source(self, doc_id: DocId) -> Any:
        """The file a document is stored in, if it is stored in one.

        None for a backend that keeps documents anywhere else, which is
        not a failure: a caller that wants a file makes one.  Asked of
        the backend rather than assumed, the way search and embedding are.
        """
        locate = getattr(self.backend, "location", None)
        return None if locate is None else locate(doc_id)

    def index(self, doc_id: DocId) -> Index:
        """The index rows a document currently yields.

        Recomputed rather than read back from the backend's tables, so it
        means the same thing for a backend that keeps no tables at all.
        """
        return index_document(doc_id, self.read(doc_id))

    def reindex(self) -> int:
        """Rebuild whatever the backend derives from its documents.

        Backends that keep no derived tables have nothing to do and say
        so, rather than the caller having to know which kind it holds.
        """
        rebuild = getattr(self.backend, "reindex_all", None)
        if rebuild is None:
            raise StoreError(
                f"{type(self.backend).__name__} keeps no derived tables"
            )
        return rebuild()

    def search(self, query: SearchQuery) -> list[SearchResult]:
        """Answer a search, if the active backend can index for one.

        Search is not on the backend Protocol -- not every store can
        answer a text or vector query, and forcing one to fake it would be
        worse than saying so.  A backend that keeps no index says that
        here rather than returning an empty list, which would read as "no
        matches" when it means "never looked".

        A semantic search uses the first embedder, of ``query.identity``
        when given, whose identity has vectors here and which answers.
        """
        if not query.semantic:
            answer = self._capability("search", "keeps no search index")
            return answer(query)
        answer = self._capability("semantic_search", "keeps no search index")
        have = self._capability("embedded_identities", "keeps no embeddings")()
        log.debug("identities with vectors: %s", ", ".join(sorted(have)) or "none")
        embedder = self.choose_embedder(query.identity, among=have)
        return answer(query, embedder)

    def embed(
        self, doc_ids: list[DocId] | None = None, identity: str | None = None
    ) -> dict[str, Any]:
        """Bring the store's embeddings up to date with its content.

        Kept out of the write path deliberately.  Embedding costs a call
        to a model that may not be running, and a write that depended on
        one would fail for a reason that has nothing to do with storing a
        document.  So this is asked for, and it is incremental: only text
        that actually changed is embedded again.

        Uses the first embedder, of IDENTITY when given, that answers, and
        says which identity it embedded for.
        """
        run = self._capability("embed", "keeps no embeddings")
        embedder = self.choose_embedder(identity)
        return {**run(embedder, doc_ids), "identity": embedder.name}

    def embedding_status(self) -> dict[str, Any]:
        """How many nodes there are, and how many vectors each identity has."""
        return self._capability("embedding_status", "keeps no embeddings")()

    def choose_embedder(
        self, identity: str | None = None, among: set[str] | None = None
    ) -> Embedder:
        """The first embedder, in order, that may be used and answers.

        IDENTITY, when given, admits only embedders of that identity;
        AMONG, when given, only those whose identity is in it -- a search
        passes the identities that have vectors, so a service whose model
        never embedded anything here is not even asked.
        """
        candidates = [
            embedder
            for embedder in self.embedders
            if (identity is None or embedder.name == identity)
            and (among is None or embedder.name in among)
        ]
        log.debug(
            "%d of %d embedding service(s) to try%s",
            len(candidates), len(self.embedders),
            f" for {identity}" if identity else "",
        )
        if not candidates:
            raise EmbeddingError(self._nothing_to_try(identity, among))
        for embedder in candidates:
            if embedder.available():
                log.debug(
                    "using %s at %s", embedder.name, getattr(embedder, "url", "?")
                )
                return embedder
        names = ", ".join(
            f"{embedder.name} at {getattr(embedder, 'url', '?')}"
            for embedder in candidates
        )
        raise EmbeddingError(f"no embedding service answered: tried {names}")

    def _nothing_to_try(self, identity: str | None, among: set[str] | None) -> str:
        if not self.embedders:
            return "no embedding service is configured"
        if identity is not None and all(e.name != identity for e in self.embedders):
            return f"no embedding service is configured for {identity}"
        if among is not None:
            which = identity or "any configured identity"
            return f"no vectors for {which} in this store; run embed first"
        return "no embedding service to try"

    def _capability(self, name: str, missing: str):
        """The backend's NAME method, or a StoreError saying it has none."""
        found = getattr(self.backend, name, None)
        if found is None:
            raise StoreError(f"{type(self.backend).__name__} {missing}")
        return found

    def briefs(self) -> list[DocumentSummary]:
        """A short summary of every document, for listing.

        Only the fields a listing shows are filled in; the counts each
        need a full walk of the document, which is far too much work to do
        for every document just to print a title.
        """
        return [self._brief(doc_id) for doc_id in self.backend.list_documents()]

    def summarise(self, doc_id: DocId) -> DocumentSummary:
        """Everything the detail view of one document shows."""
        doc = self.read(doc_id)
        index = index_document(doc_id, doc)
        return DocumentSummary(
            doc_id=doc_id,
            title=document_title(doc),
            filetags=list(doc.properties.filetags),
            schema_version=doc.schema_version,
            content_hash=self.backend.content_hash(doc_id),
            nodes=len(index.nodes),
            headings=sum(1 for row in index.nodes if row.type == "headline"),
            tags=sorted({row.tag for row in index.tags}),
            links=len(index.links),
            timestamps=len(index.timestamps),
        )

    def summarise_node(self, doc_id: DocId, ref: str) -> NodeSummary:
        """Everything the view of one node shows."""
        node = self.read_node(doc_id, ref)
        return NodeSummary(
            doc_id=doc_id, ref=ref, type=node.type, text=node_text(node)
        )

    # -- internals ----------------------------------------------------------

    def _brief(self, doc_id: DocId) -> DocumentSummary:
        """One line of a listing, or why that document has none.

        A store is a directory or a database that other things write to,
        so one document in it may be unreadable -- a file in some other
        format, or one the parser cannot manage.  Listing is how somebody
        finds out what is there, and it would be a poor answer to that
        question to fail entirely because of one of them.
        """
        try:
            doc = self.backend.read_document(doc_id)
        except StoreError as exc:
            return DocumentSummary(doc_id=doc_id, title="", error=str(exc))
        return DocumentSummary(
            doc_id=doc_id,
            title=document_title(doc),
            filetags=list(doc.properties.filetags),
            schema_version=doc.schema_version,
        )

    def _locate(self, doc_id: DocId, ref: str):
        """The document and the location of REF within it."""
        doc = self.read(doc_id)
        spot = find(doc, ref)
        if spot is None:
            raise NodeNotFound(doc_id, ref)
        return doc, spot

    def _write_back(self, doc_id: DocId, doc: OrgDocument) -> None:
        """Store an edited document, refreshing every ref in it.

        The document was just read and is about to be discarded, so refs
        are assigned in place rather than into a copy nobody would see.
        """
        self.backend.replace_document(
            doc_id, assign_refs(doc, doc_id, copy=False)
        )


def _coerce(doc: OrgDocument | dict[str, Any]) -> OrgDocument:
    if isinstance(doc, OrgDocument):
        return doc
    try:
        return OrgDocument.model_validate(doc)
    except ValidationError as exc:
        raise InvalidDocument(
            f"not a valid org document: {exc.error_count()} validation error(s)",
            errors=exc.error_count(),
        ) from exc


def _coerce_node(node: OrgNodeBase | dict[str, Any]) -> OrgNodeBase:
    if isinstance(node, OrgNodeBase):
        return node
    # Route a raw dict through a document so the discriminated union picks
    # the right class for its type, rather than guessing here.
    (parsed,) = _coerce({"contents": [node]}).contents
    if isinstance(parsed, str):
        raise InvalidDocument("a node was expected, not text")
    return parsed
