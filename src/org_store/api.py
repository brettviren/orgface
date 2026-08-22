# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The store as an HTTP API, and through that as MCP tools.

This is one definition serving two purposes.  Describing the operations
once, with typed requests and responses, gives an OpenAPI document for
free -- and the MCP server (:mod:`org_store.mcp`) is derived from that
document rather than hand-written, so a tool cannot drift from the
operation it stands for.

Every handler is a single call into :mod:`org_store.service`.  Nothing
here decides anything: that is the same rule the CLI follows, and it is
what lets a third adapter be added without a fourth copy of the logic.

The store is opened per request through a callable given at construction,
so this module knows nothing about configuration -- which is what keeps
:mod:`org_store` free of any dependency on the CLI that configures it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel, Field

from .model import OrgDocument
from .search import SearchQuery, SearchResult
from .service import DocumentSummary, NodeSummary, OrgStore
from .store import DocumentExists, DocumentNotFound, NodeNotFound, StoreError

#: Opens the store one request will use.
StoreProvider = Callable[[], OrgStore]


class CreateRequest(BaseModel):
    """A new document, and what to call it."""

    document: OrgDocument
    path_hint: str | None = Field(
        default=None,
        description="Preferred document id; the backend may adjust it.",
    )


class CreatedResponse(BaseModel):
    doc_id: str = Field(description="The id the document was actually stored under.")


class NodeUpdateResponse(BaseModel):
    ref: str = Field(
        description=(
            "The node's ref after the edit. Refs name positions, so an edit "
            "can change the ref of the node written and of everything after "
            "it; the ref sent may no longer name anything."
        )
    )


class EmbedResponse(BaseModel):
    nodes: int
    embedded: int
    removed: int


def create_app(provider: StoreProvider, title: str = "org store"):
    """Build the FastAPI application over the store PROVIDER opens."""
    from fastapi import Depends, FastAPI, HTTPException

    @contextmanager
    def _opened() -> Iterator[OrgStore]:
        store = provider()
        try:
            yield store
        finally:
            close = getattr(store.backend, "close", None)
            if close is not None:
                close()

    def store() -> Iterator[OrgStore]:
        with _opened() as opened:
            yield opened

    def _fail(exc: StoreError) -> HTTPException:
        """Turn a store failure into the status code that means it."""
        if isinstance(exc, (DocumentNotFound, NodeNotFound)):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, DocumentExists):
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    app = FastAPI(
        title=title,
        version="1",
        summary="Read, edit and search org documents.",
    )
    Store = Depends(store)

    # -- documents ----------------------------------------------------------

    @app.get(
        "/documents",
        operation_id="list_documents",
        response_model=list[DocumentSummary],
        summary="List every document, with its title and file tags.",
    )
    def list_documents(store: OrgStore = Store) -> Any:
        return store.briefs()

    @app.post(
        "/documents",
        operation_id="create_document",
        response_model=CreatedResponse,
        status_code=201,
        summary="Store a new document.",
    )
    def create_document(body: CreateRequest, store: OrgStore = Store) -> Any:
        try:
            return CreatedResponse(
                doc_id=store.create(body.document, path_hint=body.path_hint)
            )
        except StoreError as exc:
            raise _fail(exc) from exc

    @app.get(
        "/documents/{doc_id}",
        operation_id="read_document",
        response_model=OrgDocument,
        summary="The whole document, as canonical JSON with refs assigned.",
    )
    def read_document(doc_id: str, store: OrgStore = Store) -> Any:
        try:
            return store.read(doc_id)
        except StoreError as exc:
            raise _fail(exc) from exc

    @app.get(
        "/documents/{doc_id}/summary",
        operation_id="summarise_document",
        response_model=DocumentSummary,
        summary="Title, tags and counts for one document.",
    )
    def summarise_document(doc_id: str, store: OrgStore = Store) -> Any:
        try:
            return store.summarise(doc_id)
        except StoreError as exc:
            raise _fail(exc) from exc

    @app.get(
        "/documents/{doc_id}/text",
        operation_id="read_document_text",
        response_model=str,
        summary="The document as org text, which is what a person would read.",
    )
    def read_document_text(doc_id: str, store: OrgStore = Store) -> Any:
        from .render import render_document

        try:
            return render_document(store.read(doc_id))
        except StoreError as exc:
            raise _fail(exc) from exc

    @app.put(
        "/documents/{doc_id}",
        operation_id="replace_document",
        status_code=204,
        summary="Replace a document's content, keeping its id.",
    )
    def replace_document(
        doc_id: str, document: OrgDocument, store: OrgStore = Store
    ) -> None:
        try:
            store.replace(doc_id, document)
        except StoreError as exc:
            raise _fail(exc) from exc

    @app.delete(
        "/documents/{doc_id}",
        operation_id="delete_document",
        status_code=204,
        summary="Delete a document.",
    )
    def delete_document(doc_id: str, store: OrgStore = Store) -> None:
        try:
            store.delete(doc_id)
        except StoreError as exc:
            raise _fail(exc) from exc

    # -- nodes --------------------------------------------------------------

    @app.get(
        "/documents/{doc_id}/nodes/{ref}",
        operation_id="read_node",
        response_model=NodeSummary,
        summary="One node of a document, addressed by the ref search returns.",
    )
    def read_node(doc_id: str, ref: str, store: OrgStore = Store) -> Any:
        try:
            return store.summarise_node(doc_id, ref)
        except StoreError as exc:
            raise _fail(exc) from exc

    @app.put(
        "/documents/{doc_id}/nodes/{ref}",
        operation_id="update_node",
        response_model=NodeUpdateResponse,
        summary="Replace one node, returning the ref it has afterwards.",
    )
    def update_node(
        doc_id: str, ref: str, node: dict[str, Any], store: OrgStore = Store
    ) -> Any:
        try:
            return NodeUpdateResponse(ref=store.update_node(doc_id, ref, node))
        except StoreError as exc:
            raise _fail(exc) from exc

    @app.delete(
        "/documents/{doc_id}/nodes/{ref}",
        operation_id="delete_node",
        status_code=204,
        summary="Delete one node; the refs after it change as a result.",
    )
    def delete_node(doc_id: str, ref: str, store: OrgStore = Store) -> None:
        try:
            store.delete_node(doc_id, ref)
        except StoreError as exc:
            raise _fail(exc) from exc

    # -- search -------------------------------------------------------------

    @app.post(
        "/search",
        operation_id="search",
        response_model=list[SearchResult],
        summary=(
            "Search the store. Words are matched together and taken "
            "literally; set semantic to match by meaning instead. Tag and "
            "property filters are inherited the way org inherits them."
        ),
    )
    def search(query: SearchQuery, store: OrgStore = Store) -> Any:
        try:
            return store.search(query)
        except StoreError as exc:
            raise _fail(exc) from exc

    @app.post(
        "/embed",
        operation_id="embed",
        response_model=EmbedResponse,
        summary="Build the embeddings semantic search needs, incrementally.",
    )
    def embed(store: OrgStore = Store) -> Any:
        try:
            return EmbedResponse(**store.embed())
        except StoreError as exc:
            raise _fail(exc) from exc

    return app
