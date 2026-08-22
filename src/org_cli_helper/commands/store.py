# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org store`` -- keep org documents in the content store.

Documents are read and written as canonical JSON.  That is all this phase
of the store can do: turning org text into the canonical form needs either
the Emacs exporter or the standalone parser, and neither is wired up yet,
so there is deliberately no ``store add somefile.org`` here to imply
otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from .. import ui
from ..cli import Context, pass_context
from . import HELP, colour_option, opened_store


@click.group(context_settings=HELP, no_args_is_help=True)
def store() -> None:
    """Create, read, update and delete documents in the store."""


@store.command(context_settings=HELP, no_args_is_help=True)
@click.argument(
    "source", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--id",
    "doc_id",
    metavar="ID",
    help="Store under this id instead of a generated one.",
)
@pass_context
def add(ctx: Context, source: Path, doc_id: str | None) -> None:
    """Add a document to the store.

    SOURCE is either an org file, read with the standalone parser and so
    needing no Emacs, or a JSON file in the canonical schema such as
    ox-json's exporter produces. Refs are recomputed on the way in, so
    whatever produced the file, the nodes end up addressable the same way.
    """
    with opened_store(ctx) as store_:
        click.echo(
            store_.create(_read(source), path_hint=doc_id or source.name)
        )


def _read(source: Path):
    """Read SOURCE as whichever of the two accepted forms it is."""
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise click.ClickException(f"{source}: {exc.strerror or exc}") from exc

    if source.suffix.lower() == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"{source}: {exc}") from exc

    from org_store.adapters.orgparser import parse

    return parse(text)


@store.command("list", context_settings=HELP)
@colour_option
@pass_context
def list_documents(ctx: Context, colour: str) -> None:
    """List the documents in the store."""
    with opened_store(ctx) as store_:
        summaries = store_.briefs()
    use_colour = ui.resolve_colour(colour)
    ui.write(ui.format_store_list(summaries, use_colour) + "\n", colour=use_colour)


@store.command(context_settings=HELP, no_args_is_help=True)
@click.argument("doc_id", metavar="ID")
@click.option("--json", "as_json", is_flag=True, help="Print the canonical JSON.")
@colour_option
@pass_context
def show(ctx: Context, doc_id: str, as_json: bool, colour: str) -> None:
    """Show one document, summarised or in full."""
    with opened_store(ctx) as store_:
        if as_json:
            payload = store_.read(doc_id).dump()
            click.echo(json.dumps(payload, indent=2, sort_keys=True))
            return
        summary = store_.summarise(doc_id)

    use_colour = ui.resolve_colour(colour)
    ui.write(ui.format_store_summary(summary, use_colour) + "\n", colour=use_colour)


@store.command("rm", context_settings=HELP, no_args_is_help=True)
@click.argument("doc_id", metavar="ID")
@pass_context
def remove(ctx: Context, doc_id: str) -> None:
    """Delete a document and everything derived from it."""
    with opened_store(ctx) as store_:
        store_.delete(doc_id)
    click.echo(f"deleted {doc_id}")


@store.command(context_settings=HELP, no_args_is_help=True)
@click.argument("doc_id", metavar="ID")
@click.argument("ref")
@click.option("--json", "as_json", is_flag=True, help="Print the node as JSON.")
@colour_option
@pass_context
def node(ctx: Context, doc_id: str, ref: str, as_json: bool, colour: str) -> None:
    """Show one node of a document, addressed by its ref."""
    with opened_store(ctx) as store_:
        if as_json:
            found = store_.read_node(doc_id, ref)
            payload = found.model_dump(mode="json", by_alias=True)
            click.echo(json.dumps(payload, indent=2, sort_keys=True))
            return
        summary = store_.summarise_node(doc_id, ref)

    use_colour = ui.resolve_colour(colour)
    ui.write(ui.format_node(summary, use_colour) + "\n", colour=use_colour)


@store.command("rm-node", context_settings=HELP, no_args_is_help=True)
@click.argument("doc_id", metavar="ID")
@click.argument("ref")
@pass_context
def remove_node(ctx: Context, doc_id: str, ref: str) -> None:
    """Delete one node from a document.

    Refs name positions, so the refs of later nodes change as a result.
    Re-read the document before using a ref taken before this ran.
    """
    with opened_store(ctx) as store_:
        store_.delete_node(doc_id, ref)
    click.echo(f"deleted {ref}; refs after it have changed")


@store.command(context_settings=HELP)
@pass_context
def reindex(ctx: Context) -> None:
    """Rebuild the derived tables from the stored documents.

    The indexes are computed from the canonical JSON, so this restores
    them exactly after the extraction rules change.
    """
    with opened_store(ctx) as store_:
        count = store_.reindex()
    click.echo(f"reindexed {count} document(s)")
