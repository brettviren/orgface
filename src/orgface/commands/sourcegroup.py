# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The command group shared by ``slurp`` and ``orger``.

Its subcommands are not written out one by one: they are whatever sources
are registered, bundled or distributed on their own.  A source whose
dependencies are missing still appears, so it can be found, and says what
to install when it is run.
"""

from __future__ import annotations

import click

from ..cli import Context
from ..config import ConfigError, check_section
from ..sources import Kind, Source, registry
from . import HELP


class SourceGroup(click.Group):
    """A group whose commands are the registered sources of one kind."""

    def __init__(self, kind: Kind, **attrs) -> None:
        attrs.setdefault("context_settings", HELP)
        attrs.setdefault("no_args_is_help", True)
        super().__init__(**attrs)
        self.kind: Kind = kind
        self.params.append(
            click.Option(
                ["--list", "list_sources"],
                is_flag=True,
                is_eager=True,
                expose_value=False,
                callback=self._list,
                help="List the available sources, and whether each is installed.",
            )
        )

    def _list(self, ctx: click.Context, param: click.Parameter, value: bool) -> None:
        if value and not ctx.resilient_parsing:
            click.echo(format_listing(registry(self.kind).values()), nl=False)
            ctx.exit()

    def invoke(self, ctx: click.Context):
        # A source asks for its settings when it runs, so a bad value or a
        # misspelt key surfaces here.  Reporting it once, for all sources,
        # saves every one of them, including those written elsewhere, from
        # having to remember to.
        try:
            self._check_tables(ctx)
            return super().invoke(ctx)
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc

    def _check_tables(self, ctx: click.Context) -> None:
        """Check every table of every source, not only the one being run.

        A source has a table for each of its halves, so a setting put in
        the wrong half -- ``pandoc`` under ``[slurp.github]`` -- would
        otherwise be ignored by the half that was meant to use it, which
        reads only its own.
        """
        context = ctx.find_object(Context)
        if context is None:
            return
        for kind in ("slurp", "orger"):
            for source in registry(kind).values():
                check_section(context.config, source.section, source.settings)

    def list_commands(self, ctx: click.Context) -> list[str]:
        return list(registry(self.kind))

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        source = registry(self.kind).get(cmd_name)
        if source is None:
            return None
        if source.installed:
            return source.command
        return _unavailable(source)


def _unavailable(source: Source) -> click.Command:
    """Stand in for a source that cannot run, to say why."""

    def explain(**_: object) -> None:
        raise click.ClickException(
            f"{source.kind} {source.name} needs {', '.join(source.missing())}, "
            f"which is not installed; {source.install_hint()}"
        )

    return click.Command(
        source.name,
        callback=explain,
        short_help=f"{source.summary} [not installed]",
        help=f"{source.summary}\n\nNot installed: {source.install_hint()}",
        context_settings={**HELP, "ignore_unknown_options": True, "allow_extra_args": True},
    )


def format_listing(sources) -> str:
    """One line per source: name, state, where it comes from, what it is."""
    rows = [
        (
            source.name,
            "installed" if source.installed else "missing",
            source.origin,
            source.summary if source.installed else f"{source.summary} ({source.install_hint()})",
        )
        for source in sources
    ]
    if not rows:
        return "no sources\n"
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    lines = []
    for name, state, origin, summary in rows:
        state_cell = click.style(f"{state:<{widths[1]}}", fg="green" if state == "installed" else "yellow")
        lines.append(f"{name:<{widths[0]}}  {state_cell}  {origin:<{widths[2]}}  {summary}")
    return "\n".join(lines) + "\n"
