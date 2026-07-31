# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org commit`` -- write an entry into today's org-roam daily note."""

from __future__ import annotations

from pathlib import Path

import click

from .. import templates as tmpl
from .. import ui
from ..cli import Context, pass_context
from ..emacs import DaemonError, Emacs


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-m",
    "--message",
    "messages",
    metavar="TEXT",
    multiple=True,
    help="Entry text. The first -m is the title, later ones form the body. "
    "Without any -m an interactive capture is opened instead.",
)
@click.option(
    "-t",
    "--template",
    "template_spec",
    metavar="SPEC",
    help="Dailies capture template to use, named by its key, its 0-based "
    "index, or its description. Defaults to the first template.",
)
@click.option(
    "--list-templates",
    is_flag=True,
    help="List the dailies capture templates the daemon offers and exit.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the capture text that would be handed to Emacs and exit.",
)
@click.option(
    "--gui",
    is_flag=True,
    help="Open an interactive capture in a new GUI frame rather than the tty.",
)
@click.option("-q", "--quiet", is_flag=True, help="Suppress the success summary.")
@pass_context
def commit(
    ctx: Context,
    messages: tuple[str, ...],
    template_spec: str | None,
    list_templates: bool,
    dry_run: bool,
    gui: bool,
    quiet: bool,
) -> None:
    """Record an entry in today's daily note, in the style of git commit.

    The first -m becomes the entry title and any further -m options become
    its body, so:

        org commit -m "Fixed the thing" -m "Took all day."
    """
    emacs = Emacs(ctx.config)

    try:
        if list_templates:
            available = tmpl.selectable(_templates(emacs))
            click.echo(ui.format_templates(available))
            return

        if not messages:
            if dry_run:
                raise click.UsageError("--dry-run needs at least one -m")
            keys = None
            if template_spec is not None:
                keys = tmpl.select(_templates(emacs), template_spec).key
            emacs.capture_interactive(keys, gui=gui)
            return

        template = tmpl.select(_templates(emacs), template_spec)
        if template.body_kind not in {"string", "nil"}:
            raise click.ClickException(
                f"template {template.key!r} builds its text from a "
                f"{template.body_kind}, which cannot be filled from the "
                f"command line; run 'commit' without -m to capture "
                f"interactively"
            )

        title = messages[0].strip()
        if not title:
            raise click.ClickException("the first -m must not be empty")
        body = tmpl.join_body(messages[1:])
        filled = tmpl.fill(template, title, body)

        for warning in filled.warnings:
            click.echo(f"warning: {warning}", err=True)

        if dry_run:
            click.echo(filled.text)
            return

        result = emacs.commit(template.key, filled.text)
    except tmpl.TemplateError as exc:
        raise click.ClickException(str(exc)) from exc
    except DaemonError as exc:
        raise click.ClickException(str(exc)) from exc

    if quiet:
        return

    written = result.get("file")
    where = Path(written).name if written else "today's note"
    click.echo(f"[{where}] {title}")
    if written:
        click.echo(f" 1 entry added to {_tildify(Path(written))}")


def _templates(emacs: Emacs) -> list[tmpl.Template]:
    try:
        info = emacs.info()
    except DaemonError as exc:
        raise click.ClickException(str(exc)) from exc
    return tmpl.parse_templates(info.get("templates") or [])


def _tildify(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)
