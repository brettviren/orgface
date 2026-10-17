# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""GitHub as a source: ``orgface slurp github`` and ``orgface orger github``.

Needs the ``github`` extra: ghexport to fetch, and orger with HPI to make
org.  See :mod:`.core`.
"""

from .. import Source
from . import cli, core

SLURP = Source(
    kind="slurp",
    name="github",
    summary="GitHub account activity, from its API and from an account export",
    command=cli.slurp,
    settings=core.SLURP_SETTINGS,
    requires=("ghexport",),
)

ORGER = Source(
    kind="orger",
    name="github",
    summary="GitHub activity, as an org file",
    command=cli.orger,
    settings=core.ORGER_SETTINGS,
    requires=("orger", "my"),
    extra="github",
)

__all__ = ["ORGER", "SLURP"]
