# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Batching and parallel requests, and backing off when they are too much.

The service here is a small threaded HTTP server with limits of its own:
a largest batch, a most-at-once, and a number of times to say it is busy.
Each vector encodes the number in its text, so order is checked exactly.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from org_store.embed import EmbeddingError, LlamaCppEmbedder, RequestFailed


class Limits:
    def __init__(
        self,
        max_batch: int | None = None,
        max_concurrent: int | None = None,
        busy: int = 0,
        status_too_big: int = 413,
        delay: float = 0.005,
    ) -> None:
        self.max_batch = max_batch
        self.max_concurrent = max_concurrent
        self.busy = busy
        self.status_too_big = status_too_big
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.most_active = 0
        self.sizes: list[int] = []


@pytest.fixture
def limited():
    """Start a service with the given limits; yields a factory for one."""
    servers = []

    def start(limits: Limits) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                inputs = body["input"]
                with limits.lock:
                    limits.active += 1
                    limits.most_active = max(limits.most_active, limits.active)
                    crowded = (
                        limits.max_concurrent is not None
                        and limits.active > limits.max_concurrent
                    )
                    busy = limits.busy > 0
                    if busy:
                        limits.busy -= 1
                try:
                    time.sleep(limits.delay)
                    if crowded or busy:
                        self.reply(429, {"error": "slow down"}, {"Retry-After": "0"})
                        return
                    if limits.max_batch is not None and len(inputs) > limits.max_batch:
                        self.reply(limits.status_too_big, {"error": "too many inputs"})
                        return
                    limits.sizes.append(len(inputs))
                    rows = [
                        {"index": i, "embedding": [float(text.split()[-1]), 0, 0, 0]}
                        for i, text in enumerate(inputs)
                    ]
                    self.reply(200, {"data": rows})
                finally:
                    with limits.lock:
                        limits.active -= 1

            def reply(self, status, payload, headers=None) -> None:
                answer = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


TEXTS = [str(n) for n in range(100)]


def embedded(vectors) -> list[int]:
    return [int(vector[0]) for vector in vectors]


def test_batches_are_the_configured_size(limited) -> None:
    limits = Limits()
    model = LlamaCppEmbedder(url=limited(limits), dimensions=4, batch=30)
    assert embedded(model.embed_documents(TEXTS)) == list(range(100))
    assert limits.sizes == [30, 30, 30, 10]


def test_parallel_requests_keep_their_order(limited) -> None:
    limits = Limits(delay=0.05)
    model = LlamaCppEmbedder(url=limited(limits), dimensions=4, batch=10, parallel=4)
    assert embedded(model.embed_documents(TEXTS)) == list(range(100))
    assert limits.most_active > 1


def test_a_batch_too_big_is_lowered_with_a_warning(limited, caplog) -> None:
    limits = Limits(max_batch=12)
    model = LlamaCppEmbedder(url=limited(limits), dimensions=4, batch=50)
    with caplog.at_level(logging.WARNING, logger="org_store.embed"):
        assert embedded(model.embed_documents(TEXTS)) == list(range(100))
    assert model.batch == 12
    assert max(limits.sizes) <= 12
    assert "lowering batch to 25" in caplog.text
    assert "lowering batch to 12" in caplog.text


@pytest.mark.parametrize("status", [400, 422, 500])
def test_a_vaguer_refusal_also_lowers_the_batch(limited, status) -> None:
    limits = Limits(max_batch=8, status_too_big=status)
    model = LlamaCppEmbedder(url=limited(limits), dimensions=4, batch=32)
    assert embedded(model.embed_documents(TEXTS)) == list(range(100))
    assert model.batch == 8


def test_too_many_at_once_lowers_parallel_with_a_warning(limited, caplog) -> None:
    limits = Limits(max_concurrent=2, delay=0.2)
    model = LlamaCppEmbedder(url=limited(limits), dimensions=4, batch=10, parallel=8)
    with caplog.at_level(logging.WARNING, logger="org_store.embed"):
        assert embedded(model.embed_documents(TEXTS)) == list(range(100))
    assert model.parallel <= 2
    assert "lowering parallel to" in caplog.text


def test_a_busy_service_is_waited_for(limited, caplog) -> None:
    limits = Limits(busy=2)
    model = LlamaCppEmbedder(url=limited(limits), dimensions=4, batch=50)
    with caplog.at_level(logging.WARNING, logger="org_store.embed"):
        assert embedded(model.embed_documents(TEXTS)) == list(range(100))
    assert caplog.text.count("service is busy") == 2


def test_a_service_busy_for_too_long_is_given_up(limited) -> None:
    limits = Limits(busy=100, delay=0)
    model = LlamaCppEmbedder(url=limited(limits), dimensions=4, batch=50)
    with pytest.raises(RequestFailed, match="HTTP 429"):
        model.embed_documents(TEXTS)


def test_a_single_text_still_refused_is_an_error(limited) -> None:
    """Halving stops at one; past that the failure is not about size."""
    limits = Limits(max_batch=0, status_too_big=500)
    model = LlamaCppEmbedder(url=limited(limits), dimensions=4, batch=8)
    with pytest.raises(EmbeddingError, match="HTTP 500 for 1 input"):
        model.embed_documents(TEXTS[:8])


def test_an_absent_service_is_not_retried(caplog) -> None:
    model = LlamaCppEmbedder(url="http://127.0.0.1:9", dimensions=4, parallel=4)
    with caplog.at_level(logging.WARNING, logger="org_store.embed"):
        with pytest.raises(EmbeddingError, match="no embedding model"):
            model.embed_documents(TEXTS)
    assert "lowering" not in caplog.text


def test_nothing_to_embed_sends_nothing() -> None:
    model = LlamaCppEmbedder(url="http://127.0.0.1:9", dimensions=4)
    assert model.embed_documents([]) == []
