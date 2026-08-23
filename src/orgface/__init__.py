# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""orgface: do some Emacs org things quickly from the command line."""

from __future__ import annotations

__version__ = "0.1.0"


def main() -> None:
    """Console script entry point."""
    from .cli import main as cli_main

    cli_main()


__all__ = ["__version__", "main"]
