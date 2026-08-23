# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org serve`` and ``org mcp`` -- the store over HTTP, and as MCP tools."""

from __future__ import annotations

import click

from ..cli import Context, pass_context
from . import HELP


def _provider(ctx: Context):
    """A callable that opens the configured store, for the API to use.

    The store is opened per request rather than held open: a server that
    kept one sqlite connection for its lifetime would serialise every
    request behind it, and would go on holding a file the user may want
    to move.
    """
    from ..storage import StorageError, open_store

    def provide():
        try:
            return open_store(ctx.config)
        except StorageError as exc:
            raise click.ClickException(str(exc)) from exc

    return provide


@click.command(context_settings=HELP)
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address.")
@click.option("--port", default=8765, show_default=True, type=int, help="Bind port.")
@click.option(
    "--schema",
    is_flag=True,
    help="Print the OpenAPI document and exit, serving nothing.",
)
@pass_context
def serve(ctx: Context, host: str, port: int, schema: bool) -> None:
    """Serve the store over HTTP.

    The same operations `org store` and `org search` offer, as a typed
    JSON API. `org mcp` derives its tools from this, so the two cannot
    describe different things.

    It binds to localhost and has no authentication: this is for local
    tools, not for exposing your notes to a network.
    """
    from org_store.api import create_app

    app = create_app(_provider(ctx))

    if schema:
        import json

        click.echo(json.dumps(app.openapi(), indent=2, sort_keys=True))
        return

    import uvicorn

    click.echo(f"serving the org store on http://{host}:{port}", err=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


@click.command(context_settings=HELP)
@click.option(
    "--list-tools",
    is_flag=True,
    help="Print the tool names and exit, rather than serving.",
)
@pass_context
def mcp(ctx: Context, list_tools: bool) -> None:
    """Serve the store to an MCP client over stdin and stdout.

    The tools are derived from the HTTP API's OpenAPI document, so a tool
    and the operation it stands for are the same definition.

    Configure a client to run this command; it speaks MCP on stdio, so
    nothing is bound to a port and nothing else can reach it.
    """
    from org_store.mcp import create_server

    server = create_server(_provider(ctx))

    if list_tools:
        import anyio

        async def show() -> None:
            for tool in sorted(await server.list_tools(), key=lambda t: t.name):
                click.echo(f"{tool.name:24} {(tool.description or '').splitlines()[0]}")

        anyio.run(show)
        return

    server.run()
