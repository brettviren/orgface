# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Turning text into vectors, for search that is not word matching.

The model is behind a small protocol so it can be swapped: which one is
in use is a configuration question, and the store only needs to know how
many numbers come back and what to call the thing that produced them.

Documents and queries are embedded by *different* methods on purpose.
The model in use here, nomic-embed-text, is trained asymmetrically: a
passage is prefixed ``search_document:`` and a question
``search_query:``, and embedding both the same way measurably worsens
retrieval.  Making that a difference in the interface rather than a flag
means a caller cannot forget it, and a model without the distinction just
implements both the same way.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from .store import StoreError


class EmbeddingError(StoreError):
    """The embedding model could not be reached or did not answer usefully.

    A store error, so that every adapter already reporting those reports
    this too rather than each having to know embedding can fail.
    """


@runtime_checkable
class Embedder(Protocol):
    """What the store needs from an embedding model."""

    @property
    def name(self) -> str:
        """Identifies the model.

        Stored vectors are only comparable with others from the same
        model, so this is recorded alongside them: changing the model has
        to invalidate what the old one produced rather than silently
        mixing two vector spaces.  It names the model itself, not what one
        service calls it: two services serving the same weights under
        different names should give the same name here.
        """
        ...

    @property
    def dimensions(self) -> int:
        """How many numbers a vector has, fixed for the model's lifetime."""
        ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed passages, for storing."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a question, for searching."""
        ...

    def available(self) -> bool:
        """Whether the model answers now, without raising if it does not.

        Asked before choosing among several services for one identity, so
        it should be quick to say no.
        """
        ...


class LlamaCppEmbedder:
    """An embedding model behind an OpenAI-compatible ``/v1/embeddings``.

    Written for ``llama-server`` but equally happy with any service that
    speaks the same protocol, such as a LiteLLM proxy.  Talks plain HTTP,
    so there is nothing to install on this side and the model can be
    started, stopped and shared independently of this tool.  The ``url``
    may be given with or without its trailing ``/v1``; an ``api_key``, when
    given, is sent as a bearer token; it may be a callable, called once
    on first contact, so a costly lookup is skipped when no vector is needed.

    ``model`` is what this service calls the model, and is sent with every
    request.  ``identity`` is what the store records with the vectors, and
    defaults to ``model``; giving it lets two services that name one model
    differently share the vectors either produces.  Nothing a service
    reports identifies its weights reliably -- ``/v1/models`` lists the
    same names -- so this is a claim the configuration makes.
    """

    #: What nomic-embed-text expects in front of the two kinds of text.
    DOCUMENT_PREFIX = "search_document: "
    QUERY_PREFIX = "search_query: "

    def __init__(
        self,
        url: str = "http://127.0.0.1:8080",
        model: str = "nomic-embed-text-v1.5",
        dimensions: int = 768,
        timeout: float = 60.0,
        batch: int = 32,
        api_key: str | Callable[[], str | None] | None = None,
        identity: str | None = None,
        probe_timeout: float = 10.0,
    ) -> None:
        self.url = url.rstrip("/").removesuffix("/v1")
        self._api_key = api_key
        self.model = model
        self._name = identity or model
        self._dimensions = dimensions
        self.timeout = timeout
        self.batch = max(1, batch)
        self.probe_timeout = probe_timeout

    @property
    def api_key(self) -> str | None:
        """The bearer token, fetched on first use when given as a callable.

        Fetching late means a key kept somewhere costly to reach, such as
        a password manager that may prompt, is only asked for when the
        model is actually contacted.
        """
        if callable(self._api_key):
            self._api_key = self._api_key()
        return self._api_key

    @property
    def name(self) -> str:
        return self._name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch):
            chunk = texts[start : start + self.batch]
            out.extend(self._post([self.DOCUMENT_PREFIX + t for t in chunk]))
        return out

    def embed_query(self, text: str) -> list[float]:
        (vector,) = self._post([self.QUERY_PREFIX + text])
        return vector

    def available(self) -> bool:
        """Whether the server answers, without raising if it does not."""
        return self.probe() is None

    def probe(self) -> str | None:
        """Why the model cannot be used now, or None when it can.

        A one-word embedding rather than a look at ``/v1/models``: that
        lists names, which some servers ignore and others map to models
        on demand, where an embedding shows the service is reachable,
        accepts the key, serves the model by that name, and gives vectors
        of the configured width.  Uses the shorter ``probe_timeout``, so
        an unreachable service is passed over quickly.
        """
        try:
            self._post(["ping"], timeout=self.probe_timeout)
        except EmbeddingError as exc:
            return str(exc)
        return None

    # -- internals ----------------------------------------------------------

    def _post(
        self, inputs: list[str], timeout: float | None = None
    ) -> list[list[float]]:
        payload = json.dumps({"input": inputs, "model": self.model}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.url}/v1/embeddings", data=payload, headers=headers
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self.timeout
            ) as response:
                answer = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise EmbeddingError(
                f"no embedding model at {self.url}: {getattr(exc, 'reason', exc)}"
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise EmbeddingError(f"{self.url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise EmbeddingError(f"{self.url}: answer was not JSON: {exc}") from exc

        rows = answer.get("data")
        if not isinstance(rows, list) or len(rows) != len(inputs):
            raise EmbeddingError(
                f"{self.url}: asked for {len(inputs)} vectors, got "
                f"{len(rows) if isinstance(rows, list) else 'none'}"
            )
        # The endpoint may answer out of order, and does index its answers.
        ordered = sorted(rows, key=lambda row: row.get("index", 0))
        vectors = [row["embedding"] for row in ordered]
        for vector in vectors:
            if len(vector) != self._dimensions:
                raise EmbeddingError(
                    f"{self.model} returned {len(vector)} dimensions, "
                    f"not the {self._dimensions} configured"
                )
        return vectors
