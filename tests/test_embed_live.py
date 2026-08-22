# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Semantic search against the real embedding model.

Needs ``llama-server`` running with nomic-embed-text, so these skip when
it is not there rather than failing.  What they check is the part a
stand-in model cannot: that a real one actually retrieves by meaning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from org_cli_helper.config import Config
from org_cli_helper.storage import embedder as build_embedder
from org_store.backends import SqliteStore
from org_store.search import SearchQuery
from org_store.service import OrgStore

pytestmark = pytest.mark.embed

DATA = Path(__file__).parent / "data"

pytest.importorskip("sqlite_vec", reason="the semantic extra is not installed")


@pytest.fixture(scope="module")
def model():
    embedder = build_embedder(Config())
    if not embedder.available():
        pytest.skip(f"no embedding model at {embedder.url}")
    return embedder


@pytest.fixture
def store(tmp_path, model) -> OrgStore:
    from org_store.adapters.orgparser import parse_file

    opened = OrgStore(SqliteStore(tmp_path / "store.sqlite", embedder=model))
    for name in ("fixture.org", "fixture-timestamps.org"):
        opened.create(parse_file(DATA / name), path_hint=name)
    opened.embed()
    return opened


def test_the_model_reports_its_shape(model) -> None:
    assert model.dimensions == len(model.embed_query("anything"))
    assert model.name


def test_everything_gets_embedded(store: OrgStore) -> None:
    assert store.embedding_status()["pending"] == 0


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("physics equations", "latex-environment"),
        ("computer program", "src-block"),
    ],
)
def test_meaning_is_found_where_words_are_not(
    store: OrgStore, question: str, expected: str
) -> None:
    """The point of embeddings: none of these words are in the document."""
    assert store.search(SearchQuery(text=question)) == []

    found = store.search(SearchQuery(text=question, semantic=True, top_k=5))
    assert expected in {result.type for result in found}


def test_a_real_query_ranks_the_obvious_answer_first(store: OrgStore) -> None:
    found = store.search(
        SearchQuery(text="printing hello from code", semantic=True, top_k=3)
    )
    assert found
    assert "hello" in found[0].excerpt
