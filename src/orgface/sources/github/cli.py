# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``orgface slurp github`` and ``orgface orger github``: parsing and output only."""

from __future__ import annotations

from pathlib import Path

import click

from ...cli import pass_context
from ...commands import HELP
from ...config import resolve_section
from . import core


@click.group("github", context_settings=HELP, no_args_is_help=True)
def slurp() -> None:
    """Get GitHub account data into files.

    `recent` asks the API, which remembers only recent events.  `archive`
    keeps the metadata of the account export GitHub emails you, which
    holds everything.  Use both: the API for freshness, the archive for
    history.
    """


@slurp.command(context_settings=HELP)
@click.option("--token", metavar="TOKEN", help="API token. Visible to other users of this computer while it runs; prefer the environment or pass.")
@click.option("--token-passname", metavar="NAME", help="pass(1) entry whose first line is the token.")
@click.option(
    "-d", "--directory", "recent_directory",
    type=click.Path(file_okay=False, path_type=Path),
    help="Write the dump here instead of the configured directory.",
)
@click.option(
    "--include", multiple=True, metavar="FIELD",
    help="Fetch only this part of the data, e.g. events; repeat for several. Default: all.",
)
@click.option(
    "--traffic/--no-traffic", "include_traffic", default=None,
    help="Fetch repository traffic as well; slow, and only for repositories you can push to.",
)
@pass_context
def recent(ctx, token, token_passname, recent_directory, include, include_traffic) -> None:
    """Fetch recent activity from the GitHub API into a dated JSON file.

    The file is named `ghexport-DATE.json`, so a second fetch on the same
    day replaces the first and earlier days are kept.  The token is
    looked for, in order, on the command line, in the environment
    (ORGFACE_SLURP_GITHUB_TOKEN), in the config file as `token`, and as the
    first line of the pass entry `token_passname`.
    """
    settings = resolve_section(
        ctx.config, "slurp.github", core.SLURP_SETTINGS,
        {
            "token": token,
            "token_passname": token_passname,
            "recent_directory": recent_directory,
            "include_traffic": include_traffic,
        },
    )
    try:
        data = core.export_recent(core.token(settings), include, settings["include_traffic"])
        written = core.write_dump(data, core.recent_directory(settings))
    except core.GithubError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(written)


@slurp.command(context_settings=HELP, no_args_is_help=True)
@click.argument("archive", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "-d", "--directory", "archive_directory",
    type=click.Path(file_okay=False, path_type=Path),
    help="Unpack here instead of the configured directory.",
)
@pass_context
def archive(ctx, archive, archive_directory) -> None:
    """Keep the metadata of a GitHub account export, the `.tar.gz` GitHub emails.

    Only the JSON files at the top level of ARCHIVE are kept.  The rest,
    which is nearly all of it, being the git repositories, is skipped, so
    the archive can be deleted afterwards.  The JSON files already in the
    directory are replaced.

    Request the export under Settings, Account, Export account data.
    """
    settings = resolve_section(
        ctx.config, "slurp.github", core.SLURP_SETTINGS,
        {"archive_directory": archive_directory},
    )
    directory = core.archive_directory(settings)
    try:
        names = core.unpack_archive(archive, directory)
    except core.GithubError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"kept {len(names)} files in {directory}")


@click.command("github", context_settings=HELP)
@click.option(
    "--to", "output", type=click.Path(dir_okay=False, path_type=Path),
    help="The org file to write. Default: the configured output, else github.org.",
)
@click.option("--stdout", is_flag=True, help="Print the org text instead of writing a file.")
@click.option(
    "--pandoc/--no-pandoc", default=None,
    help="Convert bodies to org with pandoc; slow for a lot of text.",
)
@click.option(
    "--timestamps", type=click.Choice(core.TIMESTAMPS), help="The style of timestamps."
)
@pass_context
def orger(ctx, output, stdout, pandoc, timestamps) -> None:
    """Make an org file of GitHub activity.

    Reads what `slurp github` kept, through HPI, so HPI's own config must
    point at the same directories.  The file is regenerated whole every
    time and is made read-only, because edits would be lost.
    """
    settings = resolve_section(
        ctx.config, "orger.github", core.ORGER_SETTINGS,
        {"output": output, "pandoc": pandoc, "timestamps": timestamps},
    )
    try:
        written = core.render(
            to=settings["output"], stdout=stdout, pandoc=settings["pandoc"],
            timestamps=settings["timestamps"], hpi_config=settings["hpi_config"],
        )
    except core.GithubError as exc:
        raise click.ClickException(str(exc)) from exc
    if written is not None:
        click.echo(written)
