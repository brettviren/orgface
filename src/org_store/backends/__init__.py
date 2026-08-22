# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Store backends.

Each module here implements :class:`org_store.store.StoreBackend` over one
kind of storage.  Nothing outside a backend module may know which one is
in use.

Backends do not take the same settings -- one wants a database file, the
next a directory -- so each declares what it needs rather than a caller
guessing.  :func:`open_backend` is the only thing that has to know the
names, and adding a backend means adding an entry here, not editing the
code that opens one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..store import StoreError
from .emacsfs import EmacsDirectoryStore, OrgExporter
from .orgfs import OrgDirectoryStore
from .sqlite import SqliteStore


@dataclass(frozen=True)
class Backend:
    """One selectable backend, and what it takes to open one."""

    name: str
    open: Callable[..., Any]
    #: Which :class:`~org_cli_helper.config.Config` field locates it.
    setting: str
    #: Where it lives when configuration does not say, under the XDG
    #: state directory.
    default: str
    summary: str
    #: True when opening it needs an Emacs to read through.  Declared
    #: here so the caller supplying one does not have to know which
    #: backend that is.
    needs_emacs: bool = False


BACKENDS: dict[str, Backend] = {
    "sqlite": Backend(
        name="sqlite",
        open=lambda location, emacs=None: SqliteStore(location),
        setting="store_database",
        default="store.sqlite",
        summary="one sqlite database, with derived indexes and search",
    ),
    "orgfs": Backend(
        name="orgfs",
        open=lambda location, emacs=None: _needs_orgfs(OrgDirectoryStore, location),
        setting="store_directory",
        default="org",
        summary="a directory of org files, readable without this tool",
    ),
    "emacs": Backend(
        name="emacs",
        open=lambda location, emacs=None: _needs_orgfs(
            EmacsDirectoryStore, location, emacs
        ),
        setting="store_directory",
        default="org",
        summary="the same org files, read through a running Emacs",
        needs_emacs=True,
    ),
}


def _needs_orgfs(factory, *args):
    """Open the org-file store, or say which extra supplies it.

    Reading and writing org text needs ``org-parser`` and ``jinja2``,
    which are an optional extra: someone using only the sqlite store
    should not have to install a parser.  Both are imported lazily, so
    the failure surfaces here rather than at startup.
    """
    try:
        return factory(*args)
    except ImportError as exc:
        raise StoreError(
            "the 'orgfs' backend needs the orgfs extra: "
            f"install org-cli-helper[orgfs] ({exc})"
        ) from exc


def get(name: str) -> Backend:
    """The named backend, or a :class:`StoreError` naming the real ones."""
    try:
        return BACKENDS[name]
    except KeyError:
        known = ", ".join(sorted(BACKENDS))
        raise StoreError(f"unknown store backend {name!r}; known: {known}") from None


def open_backend(name: str, location: Path | str, emacs: OrgExporter | None = None):
    """Open the named backend at LOCATION.

    EMACS is required only by backends that declare ``needs_emacs``; the
    rest ignore it.
    """
    chosen = get(name)
    if chosen.needs_emacs and emacs is None:
        raise StoreError(f"the {name!r} backend needs an Emacs to read through")
    return chosen.open(location, emacs)


__all__ = [
    "BACKENDS",
    "Backend",
    "EmacsDirectoryStore",
    "OrgDirectoryStore",
    "OrgExporter",
    "SqliteStore",
    "get",
    "open_backend",
]
