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
from . import HELP, colour_option, identity_option, opened_store


@click.group(context_settings=HELP, no_args_is_help=True)
def store() -> None:
    """Create, read, update and delete documents in the store."""


@store.command(context_settings=HELP, no_args_is_help=True)
@click.argument(
    "sources",
    metavar="SOURCE...",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--id",
    "doc_id",
    metavar="ID",
    help="Store under this id instead of a generated one "
    "(only with a single SOURCE). The default is the file's full path.",
)
@pass_context
def add(ctx: Context, sources: tuple[Path, ...], doc_id: str | None) -> None:
    """Add one or more documents to the store.

    Each SOURCE is either an org file, read with the standalone parser and
    so needing no Emacs, or a JSON file in the canonical schema such as
    ox-json's exporter produces. Refs are recomputed on the way in, so
    whatever produced the file, the nodes end up addressable the same way.

    Sources are added in the order given, one id printed per line. Unless
    --id says otherwise a document is named by its file's full path, so two
    files of the same name in different directories are different
    documents. One whose id is already in the store is skipped with a warning and the run
    carries on. The first that cannot be read stops the run; those before
    it stay added.
    """
    if doc_id is not None and len(sources) > 1:
        raise click.UsageError("--id names one document; give a single SOURCE.")
    from org_store.store import DocumentExists

    with opened_store(ctx) as store_:
        for source in sources:
            try:
                added = store_.create(
                    _read(source), path_hint=doc_id or str(source.resolve())
                )
            except DocumentExists as exc:
                click.echo(f"warning: {source}: {exc}; skipped", err=True)
                continue
            click.echo(added)


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


@store.command(context_settings=HELP, no_args_is_help=True)
@click.argument("target", metavar="BACKEND[:PATH]")
@click.option(
    "--pull",
    is_flag=True,
    help="Copy from TARGET into the configured store, rather than to it.",
)
@click.option(
    "--prune",
    is_flag=True,
    help="Also delete documents the source does not have.",
)
@click.option(
    "-n",
    "--dry-run",
    is_flag=True,
    help="Report what would change and write nothing.",
)
@colour_option
@pass_context
def sync(
    ctx: Context, target: str, pull: bool, prune: bool, dry_run: bool, colour: str
) -> None:
    """Copy documents between the configured store and another one.

    TARGET names the other store as BACKEND or BACKEND:PATH, for example
    `orgfs:~/org` or just `sqlite` for that backend's usual location.

    The copy goes one way and the source always wins. Comparing two
    stores says that a document differs, not which side changed it, so a
    document edited on the destination is overwritten. Use --dry-run
    first if that matters.
    """
    from org_store.sync import sync as run_sync

    from ..storage import StorageError, describe, open_spec

    try:
        other = open_spec(ctx.config, target)
    except StorageError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        with opened_store(ctx) as mine:
            source, destination = (other, mine) if pull else (mine, other)
            report = run_sync(source, destination, prune=prune, dry_run=dry_run)
    finally:
        other.backend.close()

    use_colour = ui.resolve_colour(colour)
    where = describe(ctx.config)
    heading = f"{target} -> {where}" if pull else f"{where} -> {target}"
    ui.write(
        ui.format_sync_report(report, heading, use_colour) + "\n", colour=use_colour
    )
    if report.failed:
        raise SystemExit(1)


@store.group(context_settings=HELP, invoke_without_command=True)
@identity_option
@pass_context
@click.pass_context
def embed(click_ctx: click.Context, ctx: Context, identity: str | None) -> None:
    """
    Build the embeddings from added files that `org search --semantic` needs.

    The configured [[embed]] services are tried in order, and the first
    that answers is used; -i/--identity tries only those of that model
    identity. Vectors are kept per identity, so embedding with one model
    leaves another's alone.

    Only text that changed since it was last embedded is sent to the
    model, so running this again after an edit is cheap.
    """
    if click_ctx.invoked_subcommand is not None:
        return
    with opened_store(ctx) as store_:
        done = store_.embed(identity=identity)
    click.echo(
        f"{done['identity']}: embedded {done['embedded']} node(s), "
        f"removed {done['removed']}, {done['nodes']} in the store"
    )


@embed.command("status", context_settings=HELP)
@pass_context
def embed_status(ctx: Context) -> None:
    """Show the vectors each identity has, and which services answer.

    Every configured [[embed]] service is tried, so this can take as long
    as the slowest one takes to answer or time out.
    """
    from ..storage import embedders

    with opened_store(ctx) as store_:
        status = store_.embedding_status()
    services = [
        (service, model.probe())
        for service, model in zip(ctx.config.embed, embedders(ctx.config))
    ]
    click.echo(ui.format_embed_status(status, services))
