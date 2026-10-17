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

import json
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

    def __init__(self, name: str = "bag-of-words", up: bool = True) -> None:
        self._name = name
        self.up = up
        self.probes = 0
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

    def available(self) -> bool:
        self.probes += 1
        return self.up

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
    opened = OrgStore(SqliteStore(tmp_path / "store.sqlite"), [model])
    from org_store.adapters.orgparser import parse_file

    opened.create(parse_file(DATA / "fixture.org"), path_hint="fixture.org")
    return opened


def find(store: OrgStore, text: str, **kwargs):
    return store.search(SearchQuery(text=text, semantic=True, **kwargs))


def vectors(store: OrgStore, identity: str = "bag-of-words") -> int:
    """How many vectors IDENTITY has in STORE."""
    found = store.embedding_status()["identities"].get(identity)
    return 0 if found is None else found["vectors"]


def stocked(tmp_path, *embedders) -> OrgStore:
    """A store holding the fixture, offered EMBEDDERS in order."""
    from org_store.adapters.orgparser import parse_file

    store = OrgStore(SqliteStore(tmp_path / "store.sqlite"), embedders)
    store.create(parse_file(DATA / "fixture.org"), path_hint="a.org")
    return store


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


@pytest.mark.parametrize("suffix", ["", "/", "/v1", "/v1/"])
def test_the_url_may_include_v1(service, suffix: str) -> None:
    url, seen = service
    embedder = LlamaCppEmbedder(url=url + suffix, dimensions=4)
    assert len(embedder.embed_query("hello")) == 4
    assert seen[-1]["path"] == "/v1/embeddings"


def test_an_api_key_is_sent_as_a_bearer_token(service) -> None:
    url, seen = service
    LlamaCppEmbedder(url=url, dimensions=4, api_key="sekrit").embed_query("hi")
    assert seen[-1]["auth"] == "Bearer sekrit"


def test_a_callable_api_key_is_fetched_once(service) -> None:
    url, seen = service
    fetched = []

    def fetch() -> str:
        fetched.append(1)
        return "late"

    embedder = LlamaCppEmbedder(url=url, dimensions=4, api_key=fetch)
    assert fetched == []
    embedder.embed_query("hi")
    embedder.embed_query("again")
    assert seen[-1]["auth"] == "Bearer late"
    assert fetched == [1]


def test_the_identity_is_recorded_but_the_model_is_asked_for(service) -> None:
    url, seen = service
    embedder = LlamaCppEmbedder(
        url=url, model="nomic-embed-text-v1.5", dimensions=4,
        identity="nomic-embed-text",
    )
    embedder.embed_query("hi")
    assert seen[-1]["model"] == "nomic-embed-text-v1.5"
    assert embedder.name == "nomic-embed-text"


def test_the_identity_defaults_to_the_model() -> None:
    assert LlamaCppEmbedder(model="m").name == "m"


def test_no_api_key_sends_no_authorization(service) -> None:
    url, seen = service
    LlamaCppEmbedder(url=url, dimensions=4).embed_query("hi")
    assert seen[-1]["auth"] is None


# -- building the index -----------------------------------------------------


def test_nothing_is_embedded_until_asked(store: OrgStore) -> None:
    """A write must not depend on a model that may not be running."""
    status = store.embedding_status()
    assert status["nodes"] > 0
    assert status["identities"] == {}


def test_embedding_covers_the_store(store: OrgStore) -> None:
    done = store.embed()
    assert done["embedded"] == done["nodes"] > 0
    assert done["identity"] == "bag-of-words"
    assert vectors(store) == done["nodes"]


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
    before = vectors(store)
    store.delete("fixture.org")
    done = store.embed()
    assert done["removed"] == before
    assert vectors(store) == 0


def test_each_identity_keeps_its_own_vectors(tmp_path) -> None:
    """Vectors from two models are not in the same space, so both are kept."""
    first, second = BagOfWords("first-model"), BagOfWords("second-model")
    store = stocked(tmp_path, first, second)
    nodes = store.embed()["nodes"]
    assert store.embed(identity="second-model")["embedded"] == nodes

    assert vectors(store, "first-model") == vectors(store, "second-model") == nodes
    done = store.embed(identity="first-model")
    assert (done["embedded"], done["removed"]) == (0, 0)


def test_one_identity_under_two_model_names_shares_vectors(
    tmp_path, service
) -> None:
    """Two services naming the same model differently must not re-embed."""
    url, seen = service
    remote = LlamaCppEmbedder(
        url=url, model="nomic-embed-text", dimensions=4, identity="nomic"
    )
    local = LlamaCppEmbedder(
        url=url, model="nomic-embed-text-v1.5", dimensions=4, identity="nomic"
    )
    assert stocked(tmp_path, remote).embed()["embedded"] > 0

    done = stocked_again(tmp_path, local).embed()
    assert (done["embedded"], done["removed"]) == (0, 0)
    assert seen[-1]["model"] == "nomic-embed-text-v1.5"


def stocked_again(tmp_path, *embedders) -> OrgStore:
    """The store :func:`stocked` made, reopened with other EMBEDDERS."""
    return OrgStore(SqliteStore(tmp_path / "store.sqlite"), embedders)


def test_a_wider_model_cannot_claim_an_identity(tmp_path) -> None:
    store = stocked(tmp_path, BagOfWords("same"))
    store.embed()

    class Wider(BagOfWords):
        @property
        def dimensions(self) -> int:
            return super().dimensions + 1

    with pytest.raises(EmbeddingError, match="dimensions"):
        stocked_again(tmp_path, Wider("same")).embed()


# -- choosing a service -----------------------------------------------------


def test_the_first_service_that_answers_is_used(tmp_path) -> None:
    down, up, later = BagOfWords("m", up=False), BagOfWords("m"), BagOfWords("m")
    store = stocked(tmp_path, down, up, later)
    store.embed()
    assert down.probes == 1 and down.document_calls == []
    assert up.document_calls
    assert later.probes == 0


def test_an_identity_admits_only_its_services(tmp_path) -> None:
    first, second = BagOfWords("first"), BagOfWords("second")
    store = stocked(tmp_path, first, second)
    assert store.embed(identity="second")["identity"] == "second"
    assert first.probes == 0


def test_an_unconfigured_identity_is_named(tmp_path) -> None:
    store = stocked(tmp_path, BagOfWords("first"))
    with pytest.raises(EmbeddingError, match="configured for other"):
        store.embed(identity="other")


def test_nothing_answering_is_an_error_naming_what_was_tried(tmp_path) -> None:
    store = stocked(tmp_path, BagOfWords("a", up=False), BagOfWords("b", up=False))
    with pytest.raises(EmbeddingError, match="tried a at .*, b at"):
        store.embed()


def test_search_skips_identities_with_no_vectors(tmp_path) -> None:
    """A service whose model never embedded anything here is not asked."""
    empty, full = BagOfWords("empty"), BagOfWords("full")
    store = stocked(tmp_path, full)
    store.embed()

    store = stocked_again(tmp_path, empty, full)
    assert find(store, "footnote")
    assert empty.probes == 0 and empty.query_calls == []
    assert full.query_calls == ["footnote"]


def test_search_falls_through_to_another_service(tmp_path) -> None:
    store = stocked(tmp_path, BagOfWords("first"), BagOfWords("second"))
    store.embed(identity="first")
    store.embed(identity="second")

    down, up = BagOfWords("first", up=False), BagOfWords("second")
    assert find(stocked_again(tmp_path, down, up), "footnote")
    assert up.query_calls == ["footnote"]


def test_search_by_identity(tmp_path) -> None:
    first, second = BagOfWords("first"), BagOfWords("second")
    store = stocked(tmp_path, first, second)
    store.embed(identity="first")
    store.embed(identity="second")
    find(store, "footnote", identity="second")
    assert first.query_calls == [] and second.query_calls == ["footnote"]


def test_search_by_an_identity_with_no_vectors(tmp_path) -> None:
    store = stocked(tmp_path, BagOfWords("first"), BagOfWords("second"))
    store.embed(identity="first")
    with pytest.raises(EmbeddingError, match="no vectors for second"):
        find(store, "footnote", identity="second")


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


def test_scores_are_cosine_similarity(store: OrgStore) -> None:
    """Same direction scores 1; sharing no words scores 0, never below."""
    store.embed()
    results = find(store, "footnote")
    assert results[0].score == pytest.approx(1.0)
    assert "footnote" in results[0].excerpt
    assert results[-1].score == pytest.approx(0.0)
    assert all(0.0 <= result.score <= 1.0 + 1e-6 for result in results)


def test_the_query_is_embedded_as_a_query(
    store: OrgStore, model: BagOfWords
) -> None:
    store.embed()
    find(store, "hello")
    assert model.query_calls == ["hello"]


def test_nothing_embedded_says_so(store: OrgStore) -> None:
    """Not an empty result, which would read as "nothing is near"."""
    with pytest.raises(EmbeddingError, match="no vectors"):
        find(store, "hello")


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
    with pytest.raises(StoreError, match="no embedding service is configured"):
        store.embed()
    with pytest.raises(StoreError, match="no embedding service is configured"):
        store.search(SearchQuery(text="x", semantic=True))


def test_a_backend_that_keeps_none_says_so(tmp_path) -> None:
    from org_store.backends import OrgDirectoryStore

    store = OrgStore(OrgDirectoryStore(tmp_path / "org"), [BagOfWords()])
    with pytest.raises(StoreError, match="no embeddings"):
        store.embed()


def test_text_search_still_works_alongside(store: OrgStore) -> None:
    store.embed()
    assert store.search(SearchQuery(text="hello"))


# -- stores from before vector spaces ---------------------------------------


def test_an_old_store_loses_its_vectors_but_not_its_documents(tmp_path) -> None:
    import sqlite3

    import sqlite_vec

    path = tmp_path / "old.sqlite"
    stocked(tmp_path, BagOfWords()).backend.close()
    (tmp_path / "store.sqlite").rename(path)
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.executescript(
        "DROP TABLE embeddings;"
        "CREATE TABLE embeddings (doc_id TEXT, ref TEXT, model TEXT,"
        " text_hash TEXT, vec_id INTEGER, PRIMARY KEY (doc_id, ref));"
        "INSERT INTO embeddings VALUES ('a.org', 'x', 'm', 'h', 1);"
        "CREATE VIRTUAL TABLE vec_nodes USING vec0(embedding float[4]);"
    )
    db.commit()
    db.close()

    store = OrgStore(SqliteStore(path), [BagOfWords()])
    assert store.embedding_status()["identities"] == {}
    assert store.read("a.org")
    assert store.embed()["embedded"] > 0
    names = {
        row[0]
        for row in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")
    }
    assert "vec_nodes" not in names
