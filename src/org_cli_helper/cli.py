# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Command line entry point.

Argument parsing and process exit only.  Every command delegates to the
data modules and to :mod:`org_cli_helper.ui` for rendering.
"""

from __future__ import annotations

from pathlib import Path

import click

from . import __version__
from .config import Config, ConfigError, load

CONTEXT_SETTINGS = {
    "help_option_names": ["-h", "--help"],
    "max_content_width": 96,
}


class Context:
    """Shared state handed to every subcommand."""

    def __init__(self, config: Config) -> None:
        self.config = config


pass_context = click.make_pass_decorator(Context)


@click.group(
    context_settings=CONTEXT_SETTINGS,
    no_args_is_help=True,
    invoke_without_command=False,
)
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Read configuration from this file instead of the XDG location.",
)
@click.option(
    "-s",
    "--server-name",
    metavar="NAME",
    help="Emacs server name for the managed daemon.",
)
@click.option(
    "--lifetime",
    metavar="DURATION",
    help="Idle time before the daemon exits, e.g. 30m or 'never'.",
)
@click.option("--emacs", metavar="PROGRAM", help="The emacs executable.")
@click.option("--emacsclient", metavar="PROGRAM", help="The emacsclient executable.")
@click.version_option(__version__, "-V", "--version", prog_name="org-cli-helper")
@click.pass_context
def main(
    ctx: click.Context,
    config_path: Path | None,
    server_name: str | None,
    lifetime: str | None,
    emacs: str | None,
    emacsclient: str | None,
) -> None:
    """Do some Emacs org things quickly from the command line.

    Commands needing Emacs share a private `emacs --daemon`, started on
    first use and shut down after it has been idle for the configured
    lifetime.
    """
    overrides = {
        "server_name": server_name,
        "lifetime": lifetime,
        "emacs": emacs,
        "emacsclient": emacsclient,
    }
    try:
        config = load(path=config_path, overrides=overrides)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    ctx.obj = Context(config)


def _register() -> None:
    from .commands import commit, daemon, log, visit
    from .commands import config as config_cmd

    main.add_command(commit.commit)
    main.add_command(log.log)
    main.add_command(daemon.daemon)
    main.add_command(config_cmd.config)
    main.add_command(visit.visit)


_register()
