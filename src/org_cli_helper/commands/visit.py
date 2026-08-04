# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org visit`` -- open an org-roam daily note in Emacs."""

from __future__ import annotations

from datetime import datetime

import click

from .. import dailies, dates
from ..cli import Context, pass_context
from ..emacs import DaemonError, Emacs
from ..locations import LocationError, resolve


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("when", required=False, metavar="DATE")
@pass_context
def visit(ctx: Context, when: str | None) -> None:
    """Visit an org-roam daily note in Emacs.

    DATE is a free-form date such as 'today' (the default), 'yesterday',
    'last week', or 260801. If the daily note for that day exists it is
    opened via emacsclient; otherwise nothing is opened and a message is
    printed instead.
    """
    try:
        day = dates.parse(when or "today", now=datetime.now()).date()
    except dates.DateError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        locations = resolve(ctx.config, Emacs(ctx.config))
    except LocationError as exc:
        raise click.ClickException(str(exc)) from exc

    path = dailies.daily_path(locations.dailies_path, day)
    if not path.is_file():
        click.echo(f"no daily note for {day.isoformat()}: {path}", err=True)
        raise SystemExit(1)

    try:
        Emacs(ctx.config).visit(path)
    except DaemonError as exc:
        raise click.ClickException(str(exc)) from exc
