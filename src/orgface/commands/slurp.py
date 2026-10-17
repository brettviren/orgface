# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``orgface slurp`` -- fetch data from outside into durable local files."""

from __future__ import annotations

from .sourcegroup import SourceGroup


class Slurp(SourceGroup):
    """Get data from a SOURCE into files kept on this computer.

    Slurping is the only step that talks to the outside world.  What it
    keeps is the exporter's own output, such as dated JSON files, so it
    can be read again, and re-parsed when the parser changes, without
    fetching anything.  The `orger` commands turn it into org.

    Each source is configured in its own `[slurp.SOURCE]` table of the
    config file; see `orgface config show`.
    """


slurp = Slurp("slurp", name="slurp", help=Slurp.__doc__)
