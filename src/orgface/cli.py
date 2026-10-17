# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Command line entry point.

Argument parsing and process exit only.  Every command delegates to the
data modules and to :mod:`orgface.ui` for rendering.
"""

from __future__ import annotations

import logging
from pathlib import Path

import click

from . import __version__, logs
from .config import Config, ConfigError, load

log = logging.getLogger(__name__)

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
@click.option(
    "-l",
    "--log-sink",
    metavar="SINK",
    default="stderr",
    show_default=True,
    help="Where log messages go: 'stderr', 'stdout', or a file path.",
)
@click.option(
    "-L",
    "--log-level",
    type=click.Choice(logs.LEVELS, case_sensitive=False),
    default="info",
    show_default=True,
    help="The least severe log messages to show; 'debug' shows the steps taken.",
)
@click.version_option(__version__, "-V", "--version", prog_name="orgface")
@click.pass_context
def main(
    ctx: click.Context,
    config_path: Path | None,
    server_name: str | None,
    lifetime: str | None,
    emacs: str | None,
    emacsclient: str | None,
    log_sink: str,
    log_level: str,
) -> None:
    """Do some Emacs org things quickly from the command line.

    Commands needing Emacs share a private `emacs --daemon`, started on
    first use and shut down after it has been idle for the configured
    lifetime.
    """
    try:
        logs.setup(log_sink, log_level)
    except logs.LogError as exc:
        raise click.ClickException(str(exc)) from exc
    log.debug("orgface %s: %s", __version__, ctx.invoked_subcommand)

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
    log.debug(
        "configuration from %s",
        ", ".join(str(source) for source in config.sources) or "defaults only",
    )
    ctx.obj = Context(config)


def _register() -> None:
    from .commands import (
        commit,
        daemon,
        log,
        note,
        render,
        search,
        serve,
        store,
        visit,
    )
    from .commands import config as config_cmd

    main.add_command(commit.commit)
    main.add_command(note.note)
    main.add_command(log.log)
    main.add_command(daemon.daemon)
    main.add_command(config_cmd.config)
    main.add_command(visit.visit)
    main.add_command(store.store)
    main.add_command(search.search)
    main.add_command(render.render)
    main.add_command(serve.serve)
    main.add_command(serve.mcp)


_register()
