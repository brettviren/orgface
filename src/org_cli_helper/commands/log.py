# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org log`` -- show recent org-roam daily entries, in the style of git log."""

from __future__ import annotations

from datetime import date, datetime

import click

from .. import dailies, dates, ui
from ..cli import Context, pass_context
from ..emacs import DaemonError, Emacs
from ..locations import LocationError, resolve


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--today", is_flag=True, help="Only entries written today.")
@click.option(
    "--since",
    "--after",
    "since",
    metavar="DATE",
    help="Only entries at or after DATE, e.g. 2026-07-01 or '2 weeks ago'.",
)
@click.option(
    "--until",
    "--before",
    "until",
    metavar="DATE",
    help="Only entries at or before DATE.",
)
@click.option(
    "-n",
    "--max-count",
    type=click.IntRange(min=1),
    metavar="N",
    help="Limit output to the N most recent entries.",
)
@click.option("--oneline", is_flag=True, help="One compact line per entry.")
@click.option("--reverse", is_flag=True, help="Show oldest entries first.")
@click.option(
    "--color",
    "colour",
    type=click.Choice(["auto", "always", "never"]),
    default="auto",
    show_default=True,
    help="Colourise the output.",
)
@click.option("--no-pager", is_flag=True, help="Write straight to stdout.")
@click.option(
    "--refresh",
    is_flag=True,
    help="Re-read the org-roam locations from Emacs instead of the cache.",
)
@pass_context
def log(
    ctx: Context,
    today: bool,
    since: str | None,
    until: str | None,
    max_count: int | None,
    oneline: bool,
    reverse: bool,
    colour: str,
    no_pager: bool,
    refresh: bool,
) -> None:
    """Show recent entries from the org-roam daily notes.

    Each top level heading in a daily note is one entry, newest first.
    Daily notes are read directly from disk, so this works whether or not
    the Emacs daemon happens to be running.
    """
    if today and (since or until):
        raise click.UsageError("--today cannot be combined with --since/--until")

    now = datetime.now()
    try:
        if today:
            start, end = dates.day_bounds(date.today())
        else:
            start = dates.parse_bound(since, now=now)
            end = dates.parse_bound(until, now=now, end_of_day=True)
    except dates.DateError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        locations = resolve(ctx.config, Emacs(ctx.config), refresh=refresh)
        entries = dailies.collect(
            locations.dailies_path,
            since=start,
            until=end,
            limit=max_count,
            reverse=reverse,
        )
    except (LocationError, DaemonError, dailies.DailiesError) as exc:
        raise click.ClickException(str(exc)) from exc

    if not entries:
        click.echo("no entries found", err=True)
        raise SystemExit(1)

    use_colour = ui.resolve_colour(colour)
    text = ui.format_log(entries, oneline=oneline, colour=use_colour)

    if no_pager:
        ui.write(text + "\n", colour=use_colour)
    else:
        ui.page(text, ctx.config.pager, colour=use_colour)
