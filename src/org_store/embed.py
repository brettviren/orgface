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
from typing import Protocol, runtime_checkable


class EmbeddingError(Exception):
    """The embedding model could not be reached or did not answer usefully."""


@runtime_checkable
class Embedder(Protocol):
    """What the store needs from an embedding model."""

    @property
    def name(self) -> str:
        """Identifies the model.

        Stored vectors are only comparable with others from the same
        model, so this is recorded alongside them: changing the model has
        to invalidate what the old one produced rather than silently
        mixing two vector spaces.
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


class LlamaCppEmbedder:
    """An embedding model behind an OpenAI-compatible ``/v1/embeddings``.

    Written for ``llama-server`` but equally happy with any service that
    speaks the same protocol, such as a LiteLLM proxy.  Talks plain HTTP,
    so there is nothing to install on this side and the model can be
    started, stopped and shared independently of this tool.  The ``url``
    may be given with or without its trailing ``/v1``; an ``api_key``, when
    given, is sent as a bearer token.
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
        api_key: str | None = None,
    ) -> None:
        self.url = url.rstrip("/").removesuffix("/v1")
        self.api_key = api_key
        self._name = model
        self._dimensions = dimensions
        self.timeout = timeout
        self.batch = max(1, batch)

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
        try:
            self._post(["ping"])
        except EmbeddingError:
            return False
        return True

    # -- internals ----------------------------------------------------------

    def _post(self, inputs: list[str]) -> list[list[float]]:
        payload = json.dumps({"input": inputs, "model": self._name}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.url}/v1/embeddings", data=payload, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
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
                    f"{self._name} returned {len(vector)} dimensions, "
                    f"not the {self._dimensions} configured"
                )
        return vectors
