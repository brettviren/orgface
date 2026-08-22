# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Turning configuration into an open store.

Which backend is active, and where it keeps its content, is a
configuration question rather than a command-line one, and it is not
argument parsing -- so it is not in a command module.

Each backend declares which setting locates it, so this does not need to
know that sqlite wants a file and the org-file store wants a directory.
Adding a backend is an entry in :data:`org_store.backends.BACKENDS`, not
an edit here.
"""

from __future__ import annotations

from pathlib import Path

from org_store.backends import Backend, get, open_backend
from org_store.service import OrgStore
from org_store.store import StoreError

from . import xdgpaths
from .config import Config


class StorageError(Exception):
    """The configured store could not be opened."""


def backend(config: Config) -> Backend:
    """The backend configuration selects."""
    try:
        return get(config.store_backend)
    except StoreError as exc:
        raise StorageError(str(exc)) from exc


def location(config: Config) -> Path:
    """Where the active backend keeps its content.

    State rather than cache: this holds content, and deleting it would
    lose data rather than merely cost a re-fetch.
    """
    chosen = backend(config)
    configured = getattr(config, chosen.setting, None)
    if configured is not None:
        return Path(configured)
    return xdgpaths.state_dir() / chosen.default


def open_store(config: Config) -> OrgStore:
    """Open the configured backend and wrap it in the service layer.

    The Emacs client is built only for a backend that reads through one,
    so selecting sqlite never brings the daemon machinery with it.
    """
    chosen = backend(config)
    emacs = None
    if chosen.needs_emacs:
        from .emacs import Emacs

        emacs = Emacs(config)
    try:
        return OrgStore(open_backend(chosen.name, location(config), emacs))
    except StoreError as exc:
        raise StorageError(str(exc)) from exc
    except OSError as exc:
        raise StorageError(f"{location(config)}: {exc.strerror or exc}") from exc
