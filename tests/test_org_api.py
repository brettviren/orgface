# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The store over HTTP, and the MCP tools derived from it.

The two are tested together on purpose: the tools are generated from this
API's OpenAPI document, so what matters is that the generated surface
still answers, not that a hand-written description was copied correctly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from org_store.backends import SqliteStore
from org_store.service import OrgStore

pytest.importorskip("fastapi", reason="the service extra is not installed")
pytest.importorskip("fastmcp", reason="the service extra is not installed")

DATA = Path(__file__).parent / "data"


@pytest.fixture
def provider(tmp_path):
    """Opens a fresh store per request, as the server does."""

    def provide() -> OrgStore:
        return OrgStore(SqliteStore(tmp_path / "store.sqlite"))

    from org_store.adapters.orgparser import parse_file

    seed = provide()
    seed.create(parse_file(DATA / "fixture.org"), path_hint="fixture.org")
    seed.backend.close()
    return provide


@pytest.fixture
def client(provider):
    from fastapi.testclient import TestClient

    from org_store.api import create_app

    with TestClient(create_app(provider)) as opened:
        yield opened


def field(value, name):
    """Read a field whether the client handed back a model or a mapping."""
    return value[name] if isinstance(value, dict) else getattr(value, name)


# -- HTTP -------------------------------------------------------------------


def test_list_documents(client) -> None:
    response = client.get("/documents")
    assert response.status_code == 200
    assert response.json()[0]["title"] == "Canonical model fixture"


def test_read_document_is_the_canonical_json(client) -> None:
    payload = client.get("/documents/fixture.org").json()
    assert payload["$$data_type"] == "org-document"
    assert payload["contents"][0]["ref"]


def test_read_document_text(client) -> None:
    """What a person would read, rather than what a parser would."""
    text = client.get("/documents/fixture.org/text").json()
    assert text.startswith("#+title: Canonical model fixture")


def test_summary(client) -> None:
    summary = client.get("/documents/fixture.org/summary").json()
    assert summary["nodes"] == 54
    assert summary["headings"] == 3
    assert "urgent" in summary["tags"]


def test_create_and_delete(client) -> None:
    created = client.post(
        "/documents",
        json={
            "document": {
                "properties": {"title": ["Made over HTTP"]},
                "contents": [],
            },
            "path_hint": "made.org",
        },
    )
    assert created.status_code == 201
    doc_id = created.json()["doc_id"]
    assert client.get(f"/documents/{doc_id}/summary").json()["title"] == (
        "Made over HTTP"
    )
    assert client.delete(f"/documents/{doc_id}").status_code == 204
    assert client.get(f"/documents/{doc_id}").status_code == 404


def test_replace(client) -> None:
    body = {
        "properties": {"title": ["Replaced"]},
        "contents": [
            {"$$data_type": "org-node", "type": "paragraph", "contents": ["x"]}
        ],
    }
    assert client.put("/documents/fixture.org", json=body).status_code == 204
    assert client.get("/documents/fixture.org/summary").json()["nodes"] == 1


def test_search_and_read_the_node_it_names(client) -> None:
    results = client.post("/search", json={"text": "hello"}).json()
    assert [row["type"] for row in results] == ["src-block"]

    node = client.get(
        f"/documents/{results[0]['doc_id']}/nodes/{results[0]['ref']}"
    ).json()
    assert node["type"] == "src-block"
    assert "hello" in node["text"]


def test_search_qualifiers_are_accepted(client) -> None:
    body = {"text": "footnote", "tags": ["urgent"]}
    assert client.post("/search", json=body).json()
    body["tags"] = ["nosuchtag"]
    assert client.post("/search", json=body).json() == []


def test_update_node_returns_its_new_ref(client) -> None:
    doc = client.get("/documents/fixture.org").json()
    ref = doc["contents"][0]["ref"]
    response = client.put(
        f"/documents/fixture.org/nodes/{ref}",
        json={"$$data_type": "org-node", "type": "paragraph", "contents": ["new"]},
    )
    assert response.status_code == 200
    assert client.get(
        f"/documents/fixture.org/nodes/{response.json()['ref']}"
    ).json()["text"] == "new"


def test_delete_node(client) -> None:
    doc = client.get("/documents/fixture.org").json()
    ref = doc["contents"][0]["ref"]
    assert client.delete(f"/documents/fixture.org/nodes/{ref}").status_code == 204


def test_a_missing_thing_is_404(client) -> None:
    assert client.get("/documents/nope").status_code == 404
    assert client.get("/documents/nope/summary").status_code == 404
    assert client.get("/documents/fixture.org/nodes/orgmissing").status_code == 404


def test_a_duplicate_id_is_409(client) -> None:
    body = {"document": {"contents": []}, "path_hint": "fixture.org"}
    assert client.post("/documents", json=body).status_code == 409


def test_a_bad_document_is_rejected(client) -> None:
    body = {"document": {"contents": [{"$$data_type": "org-node"}]}}
    assert client.post("/documents", json=body).status_code == 422


# -- the OpenAPI document ---------------------------------------------------


def test_every_operation_is_named(provider) -> None:
    """Operation ids become MCP tool names, so they must be deliberate."""
    from org_store.api import create_app

    schema = create_app(provider).openapi()
    ids = {
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
    }
    assert "search" in ids
    assert "read_node" in ids
    # FastAPI's generated ids look like "read_document_documents__doc_id__get";
    # every one here is spelled out, so none of them do.
    assert not any(name.endswith("_get") for name in ids), ids


def test_the_schema_describes_the_model(provider) -> None:
    from org_store.api import create_app

    schema = create_app(provider).openapi()
    names = set(schema["components"]["schemas"])
    # A model used for both a request and a response is split in two, so
    # these appear as OrgDocument-Input and OrgDocument-Output.
    assert any(name.startswith("OrgDocument") for name in names), names
    assert any(name.startswith("SearchQuery") for name in names), names
    assert any(name.startswith("SearchResult") for name in names), names


# -- MCP --------------------------------------------------------------------


def call(server, name: str, arguments: dict):
    """Call one tool the way a client would, over an in-memory connection."""

    async def run():
        from fastmcp import Client

        async with Client(server) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(run()).data


@pytest.fixture
def server(provider):
    from org_store.mcp import create_server

    return create_server(provider)


def test_tools_are_derived_from_the_operations(server) -> None:
    async def names():
        from fastmcp import Client

        async with Client(server) as client:
            return {tool.name for tool in await client.list_tools()}

    tools = asyncio.run(names())
    assert {"list_documents", "read_document", "read_node", "search"} <= tools
    assert len(tools) >= 10


def test_a_tool_describes_itself(server) -> None:
    async def described():
        from fastmcp import Client

        async with Client(server) as client:
            return {
                tool.name: (tool.description or "")
                for tool in await client.list_tools()
            }

    descriptions = asyncio.run(described())
    assert "meaning" in descriptions["search"]
    assert descriptions["read_node"]


def test_search_takes_its_fields_directly(server) -> None:
    """The body model is flattened, so a tool call reads naturally."""

    async def schema():
        from fastmcp import Client

        async with Client(server) as client:
            (tool,) = [
                t for t in await client.list_tools() if t.name == "search"
            ]
            return tool.inputSchema["properties"]

    properties = asyncio.run(schema())
    assert {"text", "semantic", "tags", "top_k"} <= set(properties)


def test_calling_a_tool_reaches_the_store(server) -> None:
    found = call(server, "search", {"text": "hello"})
    assert [field(row, "type") for row in found] == ["src-block"]

    node = call(
        server,
        "read_node",
        {"doc_id": "fixture.org", "ref": field(found[0], "ref")},
    )
    assert "hello" in field(node, "text")


def test_tools_reach_the_same_answers_as_the_api(server, client) -> None:
    """One definition: the tool and the operation cannot disagree."""
    over_http = client.get("/documents/fixture.org/summary").json()
    over_mcp = call(server, "summarise_document", {"doc_id": "fixture.org"})
    assert field(over_mcp, "nodes") == over_http["nodes"]
    assert field(over_mcp, "title") == over_http["title"]


def test_a_failing_tool_says_why(server) -> None:
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="no such document"):
        call(server, "read_document", {"doc_id": "nope"})
