# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Freedesktop XDG base directory locations for org-cli-helper.

Pure path computation.  Nothing here creates directories or touches the
filesystem except :func:`ensure_dir`, which callers invoke explicitly.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "org-cli-helper"


def _home(env: str, default: str) -> Path:
    """Return ``$env`` if it holds an absolute path, else ``~/default``."""
    value = os.environ.get(env)
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / default


def config_home() -> Path:
    return _home("XDG_CONFIG_HOME", ".config")


def state_home() -> Path:
    return _home("XDG_STATE_HOME", ".local/state")


def cache_home() -> Path:
    return _home("XDG_CACHE_HOME", ".cache")


def runtime_dir() -> Path | None:
    """``$XDG_RUNTIME_DIR`` if set to an absolute path, else None."""
    value = os.environ.get("XDG_RUNTIME_DIR")
    if value and os.path.isabs(value):
        return Path(value)
    return None


def config_dir() -> Path:
    return config_home() / APP_NAME


def config_file() -> Path:
    return config_dir() / "config.toml"


def state_dir() -> Path:
    return state_home() / APP_NAME


def cache_dir() -> Path:
    return cache_home() / APP_NAME


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
