# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org config`` -- show where settings come from and what they resolved to."""

from __future__ import annotations

import click

from .. import cache, xdgpaths
from ..cli import Context, pass_context
from ..config import host_file, host_name, init_file, to_toml

HELP = {"help_option_names": ["-h", "--help"]}


@click.group(context_settings=HELP, no_args_is_help=True)
def config() -> None:
    """Inspect configuration and cached state."""


@config.command(context_settings=HELP)
@click.option("--env", is_flag=True, help="Also show the environment variable names.")
@click.option(
    "--secrets", is_flag=True, help="Include secret values such as API keys."
)
@pass_context
def show(ctx: Context, env: bool, secrets: bool) -> None:
    """Print the resolved configuration.

    The header names each config file that was read, in the order they
    were applied: the config file, then this host's file beside it.
    """
    resolved = ctx.config
    header = [f"config file: {source}" for source in resolved.sources]
    if not header:
        header = [f"config file: {xdgpaths.config_file()} (absent)"]
    text = to_toml(
        resolved.values(),
        header=header,
        env=env,
        secrets=secrets,
    )
    click.echo(text, nl=False)


@config.command(context_settings=HELP)
def init() -> None:
    """Create the default config file if it does not exist."""
    path = xdgpaths.config_file()
    if init_file(path):
        click.echo(f"created {path}")
    else:
        click.echo(f"{path} already exists; left unchanged")


@config.command(context_settings=HELP)
def paths() -> None:
    """Print the XDG locations in use."""
    config_file = xdgpaths.config_file()
    click.echo(f"config: {config_file}")
    click.echo(f"host:   {host_file(config_file, host_name())}")
    click.echo(f"state:  {xdgpaths.state_dir()}")
    click.echo(f"cache:  {xdgpaths.cache_dir()}")


@config.command("clear-cache", context_settings=HELP)
def clear_cache() -> None:
    """Forget cached org-roam locations."""
    click.echo("cache cleared" if cache.clear() else "cache was already empty")
