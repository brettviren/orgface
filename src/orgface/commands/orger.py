# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``orgface orger`` -- make org content from data already on this computer."""

from __future__ import annotations

from .sourcegroup import SourceGroup


class Orger(SourceGroup):
    """Make org content from a SOURCE.

    The data is whatever is already on this computer: the files a `slurp`
    command keeps, or something that needs no slurping such as a shell
    history.  Nothing here talks to the outside world.

    Each source is configured in its own `[orger.SOURCE]` table of the
    config file; see `orgface config show`.
    """


orger = Orger("orger", name="orger", help=Orger.__doc__)
