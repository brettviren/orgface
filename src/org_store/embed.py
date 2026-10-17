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
import logging
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol, runtime_checkable

from .store import StoreError

log = logging.getLogger(__name__)


class EmbeddingError(StoreError):
    """The embedding model could not be reached or did not answer usefully.

    A store error, so that every adapter already reporting those reports
    this too rather than each having to know embedding can fail.
    """


class RequestFailed(EmbeddingError):
    """One request to the service failed, with what is known about why.

    Enough to tell "asked for too much at once" from "the service is not
    there", which is what deciding to send less and try again needs.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        timed_out: bool = False,
        reset: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.timed_out = timed_out
        self.reset = reset
        self.retry_after = retry_after

    @property
    def busy(self) -> bool:
        """The service said, or acted as if, it has too much to do."""
        return self.status in BUSY_STATUSES

    @property
    def maybe_too_big(self) -> bool:
        """A failure a smaller request might not have."""
        return self.status in TOO_BIG_STATUSES or self.timed_out


#: Statuses meaning "too many requests at once": try fewer in parallel.
BUSY_STATUSES = frozenset({429, 503})

#: Statuses a request too large can draw.  413 says so outright; servers
#: also answer an oversized batch with a 400, 422 or 500, which may as well
#: be something else -- so a smaller batch is tried, down to one, before
#: it is believed.
TOO_BIG_STATUSES = frozenset({400, 413, 422, 500})

#: How many times to wait out a busy service before giving up.
BUSY_RETRIES = 5

#: The longest single wait for a busy service, in seconds.
LONGEST_WAIT = 30.0


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

    Documents are sent ``batch`` texts to a request, ``parallel`` requests
    at a time.  Either may be more than a service allows; a request failing
    in a way that suggests so lowers the number, with a warning, and the
    work carries on with the lower one -- for the rest of this embedder's
    life, since the limit will not have moved.

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
        parallel: int = 1,
    ) -> None:
        self.url = url.rstrip("/").removesuffix("/v1")
        self._api_key = api_key
        self.model = model
        self._name = identity or model
        self._dimensions = dimensions
        self.timeout = timeout
        self.batch = max(1, batch)
        self.parallel = max(1, parallel)
        self.probe_timeout = probe_timeout

    @property
    def api_key(self) -> str | None:
        """The bearer token, fetched on first use when given as a callable.

        Fetching late means a key kept somewhere costly to reach, such as
        a password manager that may prompt, is only asked for when the
        model is actually contacted.
        """
        if callable(self._api_key):
            log.debug("%s: fetching the API key", self.url)
            started = time.monotonic()
            self._api_key = self._api_key()
            log.debug(
                "%s: API key fetched in %.2fs", self.url, time.monotonic() - started
            )
        return self._api_key

    @property
    def name(self) -> str:
        return self._name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed TEXTS, ``batch`` to a request and ``parallel`` at a time.

        Requests go out in waves of up to ``parallel``.  What a wave gets
        back is kept; what failed is looked at, the batch or the
        parallelism lowered if the failure suggests one was too high, and
        tried again -- so a limit costs one failed wave, not the run.
        """
        if not texts:
            return []
        _ = self.api_key  # fetched once here, not raced for by the threads
        inputs = [self.DOCUMENT_PREFIX + text for text in texts]
        out: list[list[float] | None] = [None] * len(inputs)
        pending = deque(self._ranges(0, len(inputs)))
        waits = 0
        log.debug(
            "%s: %d text(s), batch %d, parallel %d",
            self.url, len(inputs), self.batch, self.parallel,
        )
        with ThreadPoolExecutor(max_workers=self.parallel) as pool:
            while pending:
                wave = [pending.popleft() for _ in range(min(self.parallel, len(pending)))]
                futures = [pool.submit(self._post, inputs[a:b]) for a, b in wave]
                failed: list[tuple[tuple[int, int], RequestFailed]] = []
                for (start, stop), future in zip(wave, futures):
                    try:
                        out[start:stop] = future.result()
                    except RequestFailed as exc:
                        failed.append(((start, stop), exc))
                if not failed:
                    waits = 0
                    continue
                waits = self._adapt(failed, len(wave), waits)
                retry = [
                    piece
                    for start, stop in (where for where, _ in failed)
                    for piece in self._ranges(start, stop)
                ]
                pending.extendleft(reversed(retry))
        return out  # type: ignore[return-value]  # every slot is filled

    def _ranges(self, start: int, stop: int) -> list[tuple[int, int]]:
        """``[start, stop)`` cut into pieces of at most ``batch``."""
        return [
            (at, min(at + self.batch, stop)) for at in range(start, stop, self.batch)
        ]

    def _adapt(
        self,
        failed: list[tuple[tuple[int, int], RequestFailed]],
        concurrent: int,
        waits: int,
    ) -> int:
        """Lower what a wave's failures suggest was too high, or give up.

        Returns how many times in a row a busy service has now been
        waited for.  Raises the failure when nothing is left to lower.
        """
        errors = [exc for _, exc in failed]
        largest = max(stop - start for (start, stop), _ in failed)
        busy = next((exc for exc in errors if exc.busy), None)
        crowded = next(
            (exc for exc in errors if exc.timed_out or exc.reset), None
        )

        # Too many at once shows as a busy answer, or, from a service that
        # cannot say so, as timeouts and dropped connections.
        if (busy or crowded) and concurrent > 1:
            self.parallel = max(1, concurrent // 2)
            log.warning(
                "%s: %d of %d parallel request(s) failed (%s); "
                "lowering parallel to %d and trying again",
                self.url, len(failed), concurrent, busy or crowded, self.parallel,
            )
            return waits
        if busy is not None:
            if waits >= BUSY_RETRIES:
                raise busy
            delay = busy.retry_after
            if delay is None:
                delay = 2.0**waits
            delay = min(delay, LONGEST_WAIT)
            log.warning(
                "%s: service is busy (%s); waiting %.1fs before trying again",
                self.url, busy, delay,
            )
            time.sleep(delay)
            return waits + 1
        too_big = next((exc for exc in errors if exc.maybe_too_big), None)
        if too_big is not None and largest > 1:
            self.batch = max(1, largest // 2)
            log.warning(
                "%s: a batch of %d failed (%s); lowering batch to %d and trying again",
                self.url, largest, too_big, self.batch,
            )
            return waits
        raise errors[0]

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
        log.debug(
            "probing %s for model %s (identity %s), timeout %.0fs",
            self.url, self.model, self._name, self.probe_timeout,
        )
        try:
            self._post(["ping"], timeout=self.probe_timeout)
        except EmbeddingError as exc:
            log.debug("probe of %s failed: %s", self.url, exc)
            return str(exc)
        log.debug("probe of %s answered", self.url)
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
        timeout = timeout or self.timeout
        log.debug(
            "POST %s/v1/embeddings: %d input(s), model %s, timeout %.0fs",
            self.url, len(inputs), self.model, timeout,
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                answer = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _http_failure(self.url, len(inputs), exc) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            raise RequestFailed(
                f"no embedding model at {self.url}: {reason}",
                timed_out=isinstance(reason, TimeoutError),
                reset=isinstance(reason, ConnectionResetError),
            ) from exc
        except TimeoutError as exc:
            raise RequestFailed(
                f"{self.url}: no answer within {timeout:.0f}s", timed_out=True
            ) from exc
        except OSError as exc:
            raise RequestFailed(
                f"{self.url}: {exc}",
                reset=isinstance(exc, (ConnectionResetError, ConnectionAbortedError)),
            ) from exc
        except json.JSONDecodeError as exc:
            raise EmbeddingError(f"{self.url}: answer was not JSON: {exc}") from exc

        log.debug(
            "%s answered in %.2fs", self.url, time.monotonic() - started
        )
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


def _http_failure(url: str, count: int, exc: urllib.error.HTTPError) -> RequestFailed:
    """A failed HTTP answer, keeping its status and what the server said."""
    try:
        said = exc.read().decode("utf-8", "replace").strip()
    except OSError:
        said = ""
    retry_after = None
    with_seconds = (exc.headers or {}).get("Retry-After")
    if with_seconds:
        try:
            retry_after = max(0.0, float(with_seconds))
        except ValueError:
            pass  # an HTTP date: wait the usual way instead
    message = f"{url}: HTTP {exc.code} for {count} input(s)"
    if said:
        message += f": {said[:200]}"
    return RequestFailed(message, status=exc.code, retry_after=retry_after)
