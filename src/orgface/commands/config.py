# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org config`` -- show where settings come from and what they resolved to."""

from __future__ import annotations

import click

from .. import cache, xdgpaths
from ..cli import Context, pass_context
from ..config import SETTINGS

HELP = {"help_option_names": ["-h", "--help"]}


@click.group(context_settings=HELP, no_args_is_help=True)
def config() -> None:
    """Inspect configuration and cached state."""


@config.command(context_settings=HELP)
@click.option("--env", is_flag=True, help="Also show the environment variable names.")
@pass_context
def show(ctx: Context, env: bool) -> None:
    """Print the resolved configuration."""
    resolved = ctx.config
    source = resolved.source or f"{xdgpaths.config_file()} (absent)"
    click.echo(f"# config file: {source}")
    for setting in SETTINGS:
        value = getattr(resolved, setting.name)
        shown = "" if value is None else value
        line = f"{setting.section}.{setting.key} = {shown}"
        if env:
            line = f"{line:<48} # {setting.env}"
        click.echo(line)


@config.command(context_settings=HELP)
def paths() -> None:
    """Print the XDG locations in use."""
    click.echo(f"config: {xdgpaths.config_file()}")
    click.echo(f"state:  {xdgpaths.state_dir()}")
    click.echo(f"cache:  {xdgpaths.cache_dir()}")


@config.command("clear-cache", context_settings=HELP)
def clear_cache() -> None:
    """Forget cached org-roam locations."""
    click.echo("cache cleared" if cache.clear() else "cache was already empty")
