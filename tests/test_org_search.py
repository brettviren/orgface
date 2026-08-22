# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Full-text search: query building, indexing, and the qualifiers."""

from __future__ import annotations

import pytest

from org_store.search import SearchQuery, match_expression, terms
from org_store.service import OrgStore
from org_store.store import StoreError
from org_store.text import own_text


def find(store: OrgStore, text: str, **kwargs):
    return store.search(SearchQuery(text=text, **kwargs))


def refs(results) -> set[str]:
    return {result.ref for result in results}


# -- query building ---------------------------------------------------------


def test_terms_are_quoted() -> None:
    assert terms("org mode") == ['"org"', '"mode"']


def test_punctuation_is_not_query_syntax() -> None:
    """Typed text is a search, not an expression; it cannot be a syntax error."""
    assert terms("E=mc^2") == ['"E=mc^2"']
    assert terms('say "hi"') == ['"say"', '"""hi"""']


def test_trailing_star_means_prefix() -> None:
    assert terms("head*") == ['"head"*']


def test_scope_becomes_a_column_filter() -> None:
    assert match_expression("x", "all") == '"x"'
    assert match_expression("x", "heading") == '{heading_text} : ("x")'
    assert match_expression("x", "body") == '{body_text} : ("x")'


def test_empty_text_has_no_expression() -> None:
    assert match_expression("", "all") == ""
    assert match_expression("   ", "all") == ""


# -- what gets indexed ------------------------------------------------------


def test_own_text_does_not_include_children(store: OrgStore, stored: str) -> None:
    """Each word belongs to one row, or one search returns it four times."""
    from org_store.model import walk

    doc = store.read(stored)
    headline = next(n for n in walk(doc) if n.type == "headline")
    # The headline's body lives in its section, not in the headline.
    assert own_text(headline) == ""


def test_a_removed_child_still_separates_words(
    store: OrgStore, stored: str
) -> None:
    """org-element hides the space beside a footnote reference."""
    from org_store.model import walk

    doc = store.read(stored)
    paragraph = next(
        n
        for n in walk(doc)
        if n.type == "paragraph" and "footnote reference" in own_text(n)
    )
    assert "referencein" not in own_text(paragraph)


def test_verbatim_text_is_searchable(store: OrgStore, stored: str) -> None:
    """A source block holds its text in properties, and must still be found."""
    results = find(store, "hello")
    assert [r.type for r in results] == ["src-block"]


def test_latex_is_searchable(store: OrgStore, stored: str) -> None:
    assert [r.type for r in find(store, "E=mc^2")] == ["latex-fragment"]


def test_empty_query_finds_nothing(store: OrgStore, stored: str) -> None:
    assert find(store, "") == []


def test_no_matches_is_an_empty_list(store: OrgStore, stored: str) -> None:
    assert find(store, "nothingwhatsoever") == []


# -- scope ------------------------------------------------------------------


def test_heading_scope_excludes_bodies(store: OrgStore, stored: str) -> None:
    everywhere = find(store, "headline")
    headings = find(store, "headline", scope="heading")
    assert {r.type for r in headings} == {"headline"}
    assert refs(headings) < refs(everywhere)


def test_body_scope_excludes_headings(store: OrgStore, stored: str) -> None:
    assert "headline" not in {r.type for r in find(store, "headline", scope="body")}


def test_headings_outrank_bodies(store: OrgStore, stored: str) -> None:
    """A word in a title is stronger evidence than one in prose."""
    results = find(store, "headline")
    assert results[0].type == "headline"
    assert results[0].score > results[-1].score


# -- qualifiers -------------------------------------------------------------


def test_node_type_filter(store: OrgStore, stored: str) -> None:
    assert {r.type for r in find(store, "headline", node_types=["headline"])} == {
        "headline"
    }


def test_tag_filter_is_inherited(store: OrgStore, stored: str) -> None:
    """A paragraph under a heading tagged :urgent: counts as urgent.

    Only the headline carries the tag row, so a filter that matched only
    the tagged node itself would never return the prose beneath it --
    which is what someone searching by tag actually wants.
    """
    tagged = find(store, "footnote", tags=["urgent"])
    assert tagged
    assert {r.type for r in tagged} == {"paragraph"}


def test_tag_filter_excludes_other_subtrees(store: OrgStore, stored: str) -> None:
    # "example" is under the second headline, which is not :urgent:.
    assert find(store, "literal", tags=["urgent"]) == []
    assert find(store, "literal") != []


def test_unknown_tag_matches_nothing(store: OrgStore, stored: str) -> None:
    assert find(store, "footnote", tags=["nosuchtag"]) == []


def test_property_filter_is_inherited(store: OrgStore, stored: str) -> None:
    found = find(store, "hello", property_filters={"CUSTOM_ID": "a-headline"})
    assert [r.type for r in found] == ["src-block"]
    assert find(store, "hello", property_filters={"CUSTOM_ID": "wrong"}) == []


def test_filters_combine(store: OrgStore, stored: str) -> None:
    assert find(
        store,
        "hello",
        tags=["urgent"],
        node_types=["src-block"],
        property_filters={"CUSTOM_ID": "a-headline"},
    )
    assert (
        find(store, "hello", tags=["urgent"], node_types=["paragraph"]) == []
    )


def test_document_filter(
    store: OrgStore, stored: str, timestamps_json: dict
) -> None:
    other = store.create(timestamps_json, path_hint="ts.org")
    assert refs(find(store, "meeting")) == refs(find(store, "meeting", doc_ids=[other]))
    assert find(store, "meeting", doc_ids=[stored]) == []


def test_top_k_limits_results(store: OrgStore, stored: str) -> None:
    assert len(find(store, "headline", top_k=1)) == 1


# -- upkeep -----------------------------------------------------------------


def test_index_follows_a_replace(store: OrgStore, stored: str) -> None:
    from org_store.model import OrgDocument

    assert find(store, "hello")
    store.replace(
        stored,
        OrgDocument.model_validate(
            {
                "contents": [
                    {
                        "$$data_type": "org-node",
                        "type": "paragraph",
                        "contents": ["something else entirely"],
                    }
                ]
            }
        ),
    )
    assert find(store, "hello") == []
    assert find(store, "entirely")


def test_index_follows_a_delete(store: OrgStore, stored: str) -> None:
    """The FTS table is virtual, so nothing cascades into it."""
    assert find(store, "hello")
    store.delete(stored)
    assert find(store, "hello") == []


def test_index_follows_a_node_delete(store: OrgStore, stored: str) -> None:
    from org_store.model import walk

    block = next(n for n in walk(store.read(stored)) if n.type == "src-block")
    store.delete_node(stored, block.ref)
    assert find(store, "hello") == []


def test_reindex_rebuilds_the_search_index(
    store: OrgStore, stored: str, backend
) -> None:
    backend._db.execute("DELETE FROM fts_nodes")
    backend._db.commit()
    assert find(store, "hello") == []

    store.reindex()
    assert find(store, "hello")


def test_results_point_at_real_nodes(store: OrgStore, stored: str) -> None:
    for result in find(store, "headline"):
        assert store.read_node(result.doc_id, result.ref).type == result.type


# -- what the service refuses -----------------------------------------------


def test_semantic_search_is_not_available_yet(store: OrgStore) -> None:
    """Refused rather than silently answered with a text search."""
    with pytest.raises(StoreError, match="semantic"):
        store.search(SearchQuery(text="x", semantic=True))


def test_a_backend_without_an_index_says_so() -> None:
    """An empty list would read as "no matches", not "never looked"."""

    class Bare:
        pass

    with pytest.raises(StoreError, match="no search index"):
        OrgStore(Bare()).search(SearchQuery(text="x"))
