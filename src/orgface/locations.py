# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Working out where the org-roam daily notes live.

Configuration wins, then the file cache, and only then is the daemon asked.
That ordering keeps ``org log`` fast and usable when Emacs is not running,
while still defaulting to whatever the user's init file says.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import cache
from .config import Config
from .emacs import Emacs

log = logging.getLogger(__name__)


class LocationError(Exception):
    """The daily notes directory could not be determined."""


@dataclass(frozen=True)
class Locations:
    roam_directory: Path
    dailies_directory: str
    dailies_path: Path
    origin: str  # config | cache | daemon


def _from_config(config: Config) -> Locations | None:
    if config.roam_directory is None or config.dailies_directory is None:
        return None
    roam = config.roam_directory
    return Locations(
        roam_directory=roam,
        dailies_directory=config.dailies_directory,
        dailies_path=(roam / config.dailies_directory).resolve(),
        origin="config",
    )


def _from_cache(config: Config) -> Locations | None:
    entry = cache.get(config.server_name)
    if not entry:
        return None
    try:
        return Locations(
            roam_directory=Path(entry["roam_directory"]),
            dailies_directory=entry["dailies_directory"],
            dailies_path=Path(entry["dailies_path"]),
            origin="cache",
        )
    except (KeyError, TypeError):
        return None


def from_info(info: dict, origin: str = "daemon") -> Locations:
    return Locations(
        roam_directory=Path(info["roam_directory"]),
        dailies_directory=info["dailies_directory"],
        dailies_path=Path(info["dailies_path"]),
        origin=origin,
    )


def store(config: Config, locations: Locations) -> None:
    cache.put(
        config.server_name,
        {
            "roam_directory": str(locations.roam_directory),
            "dailies_directory": locations.dailies_directory,
            "dailies_path": str(locations.dailies_path),
        },
    )


def resolve(
    config: Config,
    emacs: Emacs | None = None,
    refresh: bool = False,
    allow_daemon: bool = True,
) -> Locations:
    """Find the daily notes directory, consulting Emacs only if needed."""
    if not refresh:
        configured = _from_config(config)
        if configured is not None:
            log.debug("org-roam locations from configuration")
            return configured
        cached = _from_cache(config)
        if cached is not None:
            log.debug("org-roam locations from the cache")
            return cached

    if not allow_daemon:
        raise LocationError(
            "org-roam locations are unknown and the daemon was not consulted; "
            "set roam.directory and roam.dailies_directory in config.toml"
        )

    log.debug("asking the Emacs daemon for org-roam locations")
    emacs = emacs or Emacs(config)
    locations = from_info(emacs.info())
    store(config, locations)

    # An explicit roam.directory still overrides what Emacs reported.
    if config.roam_directory is not None:
        dailies = config.dailies_directory or locations.dailies_directory
        return Locations(
            roam_directory=config.roam_directory,
            dailies_directory=dailies,
            dailies_path=(config.roam_directory / dailies).resolve(),
            origin="config",
        )
    return locations
