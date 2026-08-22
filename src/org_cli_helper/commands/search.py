# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org search`` -- find nodes in the store by their text."""

from __future__ import annotations

import click

from .. import ui
from ..cli import Context, pass_context
from . import HELP, colour_option, opened_store


@click.command(context_settings=HELP, no_args_is_help=True)
@click.argument("words", nargs=-1, metavar="TEXT...")
@click.option(
    "--scope",
    type=click.Choice(["all", "heading", "body"]),
    default="all",
    show_default=True,
    help="Match headings only, bodies only, or both.",
)
@click.option(
    "-t",
    "--tag",
    "tags",
    multiple=True,
    metavar="TAG",
    help="Only within headings carrying TAG. Repeatable.",
)
@click.option(
    "-T",
    "--type",
    "node_types",
    multiple=True,
    metavar="TYPE",
    help="Only nodes of this type, e.g. headline or src-block. Repeatable.",
)
@click.option(
    "-p",
    "--property",
    "properties",
    multiple=True,
    metavar="KEY=VALUE",
    help="Only within headings whose property KEY is VALUE. Repeatable.",
)
@click.option(
    "-d",
    "--doc",
    "doc_ids",
    multiple=True,
    metavar="ID",
    help="Only within this document. Repeatable.",
)
@click.option(
    "-n",
    "--max-count",
    "top_k",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    metavar="N",
    help="Return at most N results.",
)
@colour_option
@pass_context
def search(
    ctx: Context,
    words: tuple[str, ...],
    scope: str,
    tags: tuple[str, ...],
    node_types: tuple[str, ...],
    properties: tuple[str, ...],
    doc_ids: tuple[str, ...],
    top_k: int,
    colour: str,
) -> None:
    """Search the store for TEXT.

    Words are matched together, so `org search org mode` finds nodes
    containing both. A trailing `*` matches by prefix. Everything else is
    taken literally, so punctuation and maths need no quoting.

    Tag and property filters are inherited the way org inherits them: a
    paragraph under a heading tagged :urgent: counts as urgent.
    """
    from org_store.search import SearchQuery

    query = SearchQuery(
        text=" ".join(words),
        scope=scope,
        tags=list(tags) or None,
        node_types=list(node_types) or None,
        property_filters=_properties(properties),
        doc_ids=list(doc_ids) or None,
        top_k=top_k,
    )

    with opened_store(ctx) as store:
        results = store.search(query)

    if not results:
        click.echo("no matches", err=True)
        raise SystemExit(1)

    use_colour = ui.resolve_colour(colour)
    ui.write(ui.format_search_results(results, use_colour) + "\n", use_colour)


def _properties(pairs: tuple[str, ...]) -> dict[str, str] | None:
    """Parse ``KEY=VALUE`` options into a mapping."""
    if not pairs:
        return None
    parsed: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise click.UsageError(f"--property wants KEY=VALUE, got {pair!r}")
        parsed[key] = value
    return parsed
