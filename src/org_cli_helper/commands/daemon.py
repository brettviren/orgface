# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org daemon`` -- inspect and control the managed Emacs daemon."""

from __future__ import annotations

import click

from ..cli import Context, pass_context
from ..emacs import DaemonError, Emacs
from ..locations import from_info, store

HELP = {"help_option_names": ["-h", "--help"]}


@click.group(context_settings=HELP, no_args_is_help=True)
def daemon() -> None:
    """Manage the Emacs daemon that backs the other commands.

    The daemon starts by itself when a command needs it and exits after the
    configured idle lifetime, so these commands are for when you want to
    take charge of that explicitly.
    """


@daemon.command(context_settings=HELP)
@pass_context
def start(ctx: Context) -> None:
    """Start the daemon if it is not already running."""
    emacs = Emacs(ctx.config)
    try:
        if emacs.is_running():
            click.echo(f"already running: {ctx.config.server_name}")
            return
        emacs.start()
        emacs.eval("t")  # bootstrap and arm the idle timer
    except DaemonError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"started: {ctx.config.server_name}")


@daemon.command(context_settings=HELP)
@pass_context
def stop(ctx: Context) -> None:
    """Shut the daemon down, saving any modified buffers first."""
    emacs = Emacs(ctx.config)
    try:
        stopped = emacs.stop()
    except DaemonError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"stopped: {ctx.config.server_name}"
        if stopped
        else f"not running: {ctx.config.server_name}"
    )


@daemon.command(context_settings=HELP)
@pass_context
def restart(ctx: Context) -> None:
    """Stop the daemon if running, then start a fresh one."""
    emacs = Emacs(ctx.config)
    try:
        emacs.stop()
        emacs.start()
        emacs.eval("t")
    except DaemonError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"restarted: {ctx.config.server_name}")


@daemon.command(context_settings=HELP)
@pass_context
def status(ctx: Context) -> None:
    """Report whether the daemon is running, without starting one."""
    emacs = Emacs(ctx.config)
    try:
        state = emacs.status()
    except DaemonError as exc:
        raise click.ClickException(str(exc)) from exc

    lifetime = ctx.config.lifetime
    click.echo(f"server:   {ctx.config.server_name}")
    click.echo(f"socket:   {emacs.socket_hint()}")
    click.echo(f"running:  {'yes' if state['running'] else 'no'}")
    if state["running"]:
        click.echo(f"pid:      {state.get('pid') or 'unknown'}")
    click.echo(
        "lifetime: " + (f"{lifetime}s of idle" if lifetime else "never expires")
    )


@daemon.command(context_settings=HELP)
@pass_context
def info(ctx: Context) -> None:
    """Show the org-roam locations and templates the daemon reports."""
    emacs = Emacs(ctx.config)
    try:
        payload = emacs.info()
    except DaemonError as exc:
        raise click.ClickException(str(exc)) from exc

    locations = from_info(payload)
    store(ctx.config, locations)

    click.echo(f"org-roam-directory:         {locations.roam_directory}")
    click.echo(f"org-roam-dailies-directory: {locations.dailies_directory}")
    click.echo(f"daily notes:                {locations.dailies_path}")
    click.echo("dailies capture templates:")
    for index, template in enumerate(payload.get("templates") or []):
        kind = "group" if template.get("group") else template.get("type", "entry")
        click.echo(
            f"  [{index}] {template.get('key', '')}"
            f"  {template.get('description', '')}  ({kind})"
        )
