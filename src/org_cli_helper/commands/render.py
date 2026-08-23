# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org render`` -- write stored documents out as some other kind of text."""

from __future__ import annotations

from pathlib import Path

import click

from .. import ui
from ..cli import Context, pass_context
from . import HELP, opened_store

#: Named here rather than imported, so that ``org render --help`` costs
#: nothing: reaching org_store builds the model for every org node type.
METHODS = ("template", "emacs")
FORMATS = ("html",)


@click.command(context_settings=HELP)
@click.argument("documents", nargs=-1, metavar="[ID]...")
@click.option(
    "-a",
    "--all",
    "everything",
    is_flag=True,
    help="Render every document in the store.",
)
@click.option(
    "-m",
    "--method",
    type=click.Choice(METHODS),
    default="template",
    show_default=True,
    help="How to render: a Jinja2 template, or org-export inside Emacs.",
)
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(FORMATS),
    default="html",
    show_default=True,
    help="What to render: picks the default template and the exporter.",
)
@click.option(
    "-t",
    "--template",
    "template",
    metavar="FILE",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Render with this Jinja2 template instead of the format's own.",
)
@click.option(
    "-o",
    "--output",
    metavar="PATH",
    help="Write here: a file, a directory, or - for standard output.",
)
@pass_context
def render(
    ctx: Context,
    documents: tuple[str, ...],
    everything: bool,
    method: str,
    fmt: str,
    template: Path | None,
    output: str | None,
) -> None:
    """Render documents from the store as HTML or another format.

    Name the documents to render, or pass -a/--all for every one of
    them. With neither, this help is what you get, since rendering
    nothing is never what was meant.

    Where the text goes follows from how much of it there is. One
    document goes to the file named by -o, or to `-` for standard
    output, or to its own name in the current directory. Several go to
    the directory named by -o, or to `output/`.

        org render notes.org                  # -> notes.html
        org render notes.org -o -             # -> standard output
        org render -a -o site/                # -> site/*.html
        org render notes.org -m emacs         # org-export, in Emacs
    """
    if not documents and not everything:
        click.echo(click.get_current_context().get_help())
        return
    if documents and everything:
        raise click.UsageError("--all renders everything; naming documents too")
    if template is not None and method != "template":
        raise click.UsageError(
            f"--template is for --method template, not --method {method}"
        )

    from org_store.export import ExportError, get_format, get_method
    from org_store.export import render as render_document

    from .. import rendering

    try:
        extension = get_format(fmt).extension
        needs_emacs = get_method(method).needs_emacs
    except ExportError as exc:
        raise click.ClickException(str(exc)) from exc

    emacs = _emacs(ctx) if needs_emacs else None

    with opened_store(ctx) as store:
        try:
            wanted = store.list() if everything else rendering.resolve(
                documents, store.list()
            )
            plan = rendering.destinations(
                wanted, output, extension, everything=everything
            )
        except rendering.RenderError as exc:
            raise click.ClickException(str(exc)) from exc

        written: list[Path] = []
        for destination in plan:
            try:
                text = render_document(
                    store.read(destination.doc_id),
                    fmt=fmt,
                    method=method,
                    template=template,
                    emacs=emacs,
                    doc_id=destination.doc_id,
                    source=store.source(destination.doc_id),
                )
            except ExportError as exc:
                raise click.ClickException(str(exc)) from exc

            if destination.to_stdout:
                click.echo(text, nl=not text.endswith("\n"))
            else:
                written.append(rendering.write(destination, text))

    if written:
        click.echo(ui.format_rendered(written))


def _emacs(ctx: Context):
    """The Emacs the 'emacs' method renders through."""
    from ..emacs import Emacs

    return Emacs(ctx.config)
