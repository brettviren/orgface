# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Embeddings: storing them, keeping them current, and searching by them.

The model here is a stand-in that embeds bag-of-words over a small fixed
vocabulary.  It is deterministic and needs nothing running, but it is a
real vector space: similar text really does come out nearer, so nearest
neighbour is genuinely exercised rather than mocked away.  The live model
is covered separately, under the ``embed`` marker.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from org_store.backends import SqliteStore
from org_store.embed import Embedder, EmbeddingError, LlamaCppEmbedder
from org_store.search import SearchQuery
from org_store.service import OrgStore
from org_store.store import StoreError

DATA = Path(__file__).parent / "data"

pytest.importorskip("sqlite_vec", reason="the semantic extra is not installed")


VOCABULARY = (
    "hello print python source block quote table footnote maths equation "
    "headline heading text example fixed meeting"
).split()


class BagOfWords:
    """A deterministic stand-in for an embedding model."""

    def __init__(self, name: str = "bag-of-words") -> None:
        self._name = name
        self.document_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def dimensions(self) -> int:
        return len(VOCABULARY)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(list(texts))
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        words = text.lower().split()
        raw = [float(words.count(term)) for term in VOCABULARY]
        length = math.sqrt(sum(value * value for value in raw)) or 1.0
        return [value / length for value in raw]


@pytest.fixture
def model() -> BagOfWords:
    return BagOfWords()


@pytest.fixture
def store(tmp_path, model: BagOfWords) -> OrgStore:
    backend = SqliteStore(tmp_path / "store.sqlite", embedder=model)
    opened = OrgStore(backend)
    from org_store.adapters.orgparser import parse_file

    opened.create(parse_file(DATA / "fixture.org"), path_hint="fixture.org")
    return opened


def find(store: OrgStore, text: str, **kwargs):
    return store.search(SearchQuery(text=text, semantic=True, **kwargs))


# -- the protocol -----------------------------------------------------------


def test_the_stand_in_satisfies_the_protocol(model: BagOfWords) -> None:
    assert isinstance(model, Embedder)


def test_the_real_embedder_satisfies_it_too() -> None:
    assert isinstance(LlamaCppEmbedder(), Embedder)


def test_documents_and_queries_are_prefixed_differently() -> None:
    """nomic-embed-text is trained asymmetrically; the prefixes matter."""
    assert LlamaCppEmbedder.DOCUMENT_PREFIX != LlamaCppEmbedder.QUERY_PREFIX
    assert "document" in LlamaCppEmbedder.DOCUMENT_PREFIX
    assert "query" in LlamaCppEmbedder.QUERY_PREFIX


def test_an_absent_model_is_reported_not_hidden() -> None:
    embedder = LlamaCppEmbedder(url="http://127.0.0.1:9", timeout=2)
    assert embedder.available() is False
    with pytest.raises(EmbeddingError, match="no embedding model"):
        embedder.embed_query("anything")


# -- building the index -----------------------------------------------------


def test_nothing_is_embedded_until_asked(store: OrgStore) -> None:
    """A write must not depend on a model that may not be running."""
    status = store.embedding_status()
    assert status["nodes"] > 0
    assert status["embedded"] == 0
    assert status["pending"] == status["nodes"]


def test_embedding_covers_the_store(store: OrgStore) -> None:
    done = store.embed()
    assert done["embedded"] == done["nodes"] > 0
    assert store.embedding_status()["pending"] == 0


def test_a_second_pass_embeds_nothing(store: OrgStore, model: BagOfWords) -> None:
    """Hash-gated: unchanged text is not sent to the model again."""
    store.embed()
    calls = len(model.document_calls)
    assert store.embed()["embedded"] == 0
    assert len(model.document_calls) == calls


def test_only_changed_text_is_re_embedded(
    store: OrgStore, model: BagOfWords
) -> None:
    store.embed()
    doc = store.read("fixture.org")
    doc.contents.append(
        type(doc).model_validate(
            {
                "contents": [
                    {
                        "$$data_type": "org-node",
                        "type": "paragraph",
                        "contents": ["a brand new meeting"],
                    }
                ]
            }
        ).contents[0]
    )
    store.replace("fixture.org", doc)

    model.document_calls.clear()
    done = store.embed()
    assert done["embedded"] == 1
    assert model.document_calls == [["a brand new meeting"]]


def test_removed_nodes_lose_their_vectors(store: OrgStore) -> None:
    store.embed()
    before = store.embedding_status()["embedded"]
    store.delete("fixture.org")
    done = store.embed()
    assert done["removed"] == before
    assert store.embedding_status()["embedded"] == 0


def test_changing_the_model_invalidates_everything(tmp_path) -> None:
    """Vectors from two models are not in the same space."""
    from org_store.adapters.orgparser import parse_file

    first = BagOfWords("first-model")
    backend = SqliteStore(tmp_path / "store.sqlite", embedder=first)
    store = OrgStore(backend)
    store.create(parse_file(DATA / "fixture.org"), path_hint="a.org")
    store.embed()
    assert store.embedding_status()["pending"] == 0

    backend.embedder = BagOfWords("second-model")
    assert store.embedding_status()["pending"] > 0
    done = store.embed()
    assert done["removed"] > 0
    assert done["embedded"] > 0


def test_a_subset_can_be_embedded(store: OrgStore) -> None:
    assert store.embed(["fixture.org"])["embedded"] > 0
    assert store.embed([])["embedded"] == 0


# -- searching --------------------------------------------------------------


def test_semantic_search_finds_by_vector(store: OrgStore) -> None:
    store.embed()
    results = find(store, "print python")
    assert results
    assert any("print" in result.excerpt for result in results)


def test_results_carry_the_same_shape_as_text_search(store: OrgStore) -> None:
    """An adapter must not need to know which mode produced a result."""
    store.embed()
    (result, *_) = find(store, "hello")
    assert result.doc_id == "fixture.org"
    assert store.read_node(result.doc_id, result.ref).type == result.type
    assert result.excerpt


def test_results_are_ordered_by_nearness(store: OrgStore) -> None:
    store.embed()
    results = find(store, "hello print python")
    scores = [result.score for result in results]
    assert scores == sorted(scores, reverse=True)


def test_the_query_is_embedded_as_a_query(
    store: OrgStore, model: BagOfWords
) -> None:
    store.embed()
    find(store, "hello")
    assert model.query_calls == ["hello"]


def test_nothing_embedded_means_no_results(store: OrgStore) -> None:
    """Honest emptiness: there is nothing to be near."""
    assert find(store, "hello") == []


def test_top_k_limits_results(store: OrgStore) -> None:
    store.embed()
    assert len(find(store, "text", top_k=2)) <= 2


def test_qualifiers_still_apply(store: OrgStore) -> None:
    """A nearest-neighbour search knows nothing of tags; the filter does."""
    store.embed()
    assert find(store, "hello", node_types=["src-block"])
    assert find(store, "hello", node_types=["table"]) == []
    assert find(store, "hello", doc_ids=["nowhere.org"]) == []
    assert find(store, "hello", tags=["urgent"])
    assert find(store, "hello", tags=["nosuchtag"]) == []


def test_property_filters_apply_too(store: OrgStore) -> None:
    store.embed()
    assert find(store, "hello", property_filters={"CUSTOM_ID": "a-headline"})
    assert find(store, "hello", property_filters={"CUSTOM_ID": "wrong"}) == []


def test_a_store_without_a_model_says_so(tmp_path) -> None:
    store = OrgStore(SqliteStore(tmp_path / "bare.sqlite"))
    with pytest.raises(StoreError, match="no embedding model"):
        store.embed()
    with pytest.raises(StoreError, match="no embedding model"):
        store.search(SearchQuery(text="x", semantic=True))


def test_a_backend_that_keeps_none_says_so(tmp_path) -> None:
    from org_store.backends import OrgDirectoryStore

    store = OrgStore(OrgDirectoryStore(tmp_path / "org"))
    with pytest.raises(StoreError, match="no embeddings"):
        store.embed()


def test_text_search_still_works_alongside(store: OrgStore) -> None:
    store.embed()
    assert store.search(SearchQuery(text="hello"))
