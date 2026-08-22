# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The store as MCP tools, derived from the HTTP API.

Nothing here describes a tool.  The tools come from
:mod:`org_store.api`'s OpenAPI document, so each one's name, arguments
and result are the operation's own -- there is no second description to
keep in step with the first, and adding a route adds a tool.

That is the whole reason the FastAPI layer is real rather than a schema
exercise: the MCP server calls it, in process, over an ASGI transport
with no socket and no port.
"""

from __future__ import annotations

from .api import StoreProvider, create_app

#: Tool names an agent sees.  Taken from the operation ids so that the
#: HTTP operation and the tool are unmistakably the same thing.
SERVER_NAME = "org-store"

INSTRUCTIONS = """\
Read, edit and search a store of org-mode documents.

A document is addressed by its id, which `list_documents` reports. Within
a document each node -- a heading, a paragraph, a source block -- has a
`ref`, which `search` returns and `read_node` takes.

Refs name positions, not identities: inserting or deleting a node changes
the refs of everything after it. Re-read a document before reusing a ref
taken before an edit. The identifier that survives editing is org's own
`ID` property.

`search` matches words by default and takes them literally, so
punctuation and maths need no escaping. Set `semantic` to match by
meaning instead; that only finds what `embed` has covered.
"""


def create_server(provider: StoreProvider, name: str = SERVER_NAME):
    """Build the MCP server over the store PROVIDER opens."""
    from fastmcp import FastMCP

    return FastMCP.from_fastapi(
        app=create_app(provider, title=name),
        name=name,
        instructions=INSTRUCTIONS,
    )
