# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org note`` -- write an entry, plus its context, into an org-roam topic."""

from __future__ import annotations

from pathlib import Path

import click

from .. import context as runctx
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
    "-T",
    "--topic",
    metavar="TOPIC",
    help="Capture into this org-roam topic instead of the configured one. "
    "The topic is created if it does not exist yet.",
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
def note(
    ctx: Context,
    messages: tuple[str, ...],
    topic: str | None,
    dry_run: bool,
    gui: bool,
    quiet: bool,
) -> None:
    """Record an entry, with the context it was written in, in an org-roam topic.

    Called like `commit`: the first -m becomes the entry title and any
    further -m options become its body. What is added is the context the
    command ran in -- the working directory, host, user and the time to the
    second -- as an org timestamp and properties on the headline:

        org note -m "Fixed the thing" -m "Took all day."

    A headline tag records where that was: "home" in your home directory,
    and the name of the enclosing git working tree, if any.

    Entries go to the topic named by note.topic ("Context Notes" unless
    configured otherwise) rather than to a daily note. Unlike `commit` the
    capture template is built here, so the dailies templates and -t do not
    apply.
    """
    topic = topic or ctx.config.note_topic
    context = runctx.gather()
    emacs = Emacs(ctx.config)

    try:
        template = runctx.note_template(context)

        if not messages:
            if dry_run:
                raise click.UsageError("--dry-run needs at least one -m")
            emacs.note_interactive(topic, template.effective_body(), gui=gui)
            return

        title = messages[0].strip()
        if not title:
            raise click.ClickException("the first -m must not be empty")
        filled = tmpl.fill(template, title, tmpl.join_body(messages[1:]))

        for warning in filled.warnings:
            click.echo(f"warning: {warning}", err=True)

        if dry_run:
            click.echo(filled.text)
            return

        result = emacs.note(topic, filled.text)
    except tmpl.TemplateError as exc:
        raise click.ClickException(str(exc)) from exc
    except DaemonError as exc:
        raise click.ClickException(str(exc)) from exc

    if quiet:
        return

    written = result.get("file")
    click.echo(
        ui.format_capture_summary(
            result.get("topic") or topic,
            title,
            Path(written) if written else None,
            context.tags,
        )
    )
