# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Where log records go, and how much of them.

Every module logs through ``logging.getLogger(__name__)``, so records
are named for where they came from (``orgface.*`` or ``org_store.*``).
This only decides the sink and the level, once, for the whole process.
Timestamps carry milliseconds, so the gap between two lines shows where
time went -- which is the point of most debug output here.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

#: Level names accepted on the command line, quietest last.
LEVELS = ("debug", "info", "warning", "error", "critical")

#: Sink names that mean a standard stream rather than a file.
STREAMS = {"stderr": lambda: sys.stderr, "stdout": lambda: sys.stdout}

FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"

#: The handler :func:`setup` installed, replaced if it is called again.
_installed: logging.Handler | None = None


class LogError(Exception):
    """The log sink could not be opened, or the level is unknown."""


def setup(sink: str = "stderr", level: str = "info") -> None:
    """Send log records at LEVEL and above to SINK.

    SINK is ``stderr``, ``stdout``, or otherwise a file path, appended to
    and created with its directory if need be.  Calling this again
    replaces what the previous call installed and nothing else, so
    handlers someone else added to the root logger are left alone.
    """
    global _installed

    name = level.lower()
    if name not in LEVELS:
        raise LogError(f"unknown log level {level!r}; known: {', '.join(LEVELS)}")

    if sink in STREAMS:
        handler: logging.Handler = logging.StreamHandler(STREAMS[sink]())
    else:
        path = Path(sink).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(path, encoding="utf-8")
        except OSError as exc:
            raise LogError(f"cannot log to {path}: {exc.strerror or exc}") from exc
    handler.setFormatter(logging.Formatter(FORMAT, DATE_FORMAT))

    root = logging.getLogger()
    if _installed is not None:
        root.removeHandler(_installed)
        _installed.close()
    root.addHandler(handler)
    root.setLevel(getattr(logging, name.upper()))
    _installed = handler
