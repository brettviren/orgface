# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org visit`` -- open an org-roam daily or topic note in Emacs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import click

from .. import dailies, dates
from ..cli import Context, pass_context
from ..emacs import DaemonError, Emacs
from ..locations import LocationError, resolve


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("words", nargs=-1, metavar="[DATE]...")
@click.option(
    "-T",
    "--topic",
    "as_topic",
    is_flag=True,
    help="Take the arguments as an org-roam topic name rather than a date. "
    "With no arguments the topic 'note' captures into is used.",
)
@pass_context
def visit(ctx: Context, words: tuple[str, ...], as_topic: bool) -> None:
    """Visit an org-roam daily note in Emacs.

    DATE is a free-form date such as 'today' (the default), 'yesterday',
    'last week', or 260801. With -T the arguments name an org-roam topic
    instead, as `note -T` takes it, and default to the configured
    note.topic. If the note exists it is opened via emacsclient; otherwise
    nothing is opened and a message is printed instead.
    """
    spec = " ".join(words)
    emacs = Emacs(ctx.config)
    finder = _topic_note if as_topic else _daily_note
    path = finder(ctx, emacs, spec)

    try:
        emacs.visit(path)
    except DaemonError as exc:
        raise click.ClickException(str(exc)) from exc


def _daily_note(ctx: Context, emacs: Emacs, spec: str) -> Path:
    """The daily note file for the date SPEC names, which must exist."""
    try:
        day = dates.parse(spec or "today", now=datetime.now()).date()
    except dates.DateError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        locations = resolve(ctx.config, emacs)
    except LocationError as exc:
        raise click.ClickException(str(exc)) from exc

    path = dailies.daily_path(locations.dailies_path, day)
    if not path.is_file():
        click.echo(f"no daily note for {day.isoformat()}: {path}", err=True)
        raise SystemExit(1)
    return path


def _topic_note(ctx: Context, emacs: Emacs, spec: str) -> Path:
    """The file of the org-roam topic SPEC names, which must exist.

    Unlike a daily note this needs the daemon: only org-roam's database
    knows which file holds a given title.
    """
    title = spec or ctx.config.note_topic
    try:
        found = emacs.topic(title)
    except DaemonError as exc:
        raise click.ClickException(str(exc)) from exc

    path = found.get("file")
    if not path:
        click.echo(f"no org-roam note titled {title!r}", err=True)
        raise SystemExit(1)
    return Path(path)
