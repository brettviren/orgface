# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""CLI subcommands.

Modules here parse and present.  All decisions live in the sibling data
modules so the same logic is reachable without Click.

Options that more than one command offers are declared here once, so the
commands stay consistent with each other by construction rather than by
everyone remembering to copy the same block.
"""

from __future__ import annotations

from contextlib import contextmanager

import click

#: Every command takes ``-h`` as well as ``--help``.
HELP = {"help_option_names": ["-h", "--help"]}


@contextmanager
def opened_store(ctx):
    """Yield the configured store, reporting its failures as CLI errors.

    The imports are deliberately deferred into the body.  Reaching
    :mod:`org_store` builds the pydantic model for every org node type,
    which costs about a quarter of a second -- and ``cli._register()``
    imports every command module on every invocation, so at module scope
    that cost would be paid by ``org commit`` and ``org log``, which never
    touch the store.  This is the only door into the store, so the one
    deferral covers every command that uses it.
    """
    from org_store.store import StoreError

    from ..storage import StorageError, open_store

    try:
        store = open_store(ctx.config)
    except StorageError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        yield store
    except StoreError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        store.backend.close()

#: Shared by every command that writes styled output.
colour_option = click.option(
    "--color",
    "colour",
    type=click.Choice(["auto", "always", "never"]),
    default="auto",
    show_default=True,
    help="Colourise the output.",
)
