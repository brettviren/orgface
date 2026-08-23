# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""XDG file cache for values that are expensive to ask Emacs for.

Only org-roam's locations are cached.  They change about as often as a
person edits their init file, so entries are keyed by server name and
refreshed on demand rather than expiring on a timer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import xdgpaths

VERSION = 1


def path() -> Path:
    return xdgpaths.cache_dir() / "locations.json"


def _read() -> dict[str, Any]:
    try:
        with path().open(encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def get(key: str) -> dict[str, Any] | None:
    value = _read().get(key)
    return value if isinstance(value, dict) else None


def put(key: str, value: dict[str, Any]) -> None:
    entries = _read()
    entries[key] = value
    target = path()
    try:
        xdgpaths.ensure_dir(target.parent)
        tmp = target.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump({"version": VERSION, "entries": entries}, stream, indent=1)
        tmp.replace(target)
    except OSError:
        pass  # a warm cache is an optimisation, never a requirement


def clear() -> bool:
    """Remove the cache file; False if there was nothing to remove."""
    try:
        path().unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
