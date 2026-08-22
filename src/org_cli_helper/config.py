# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Layered configuration for org-cli-helper.

Settings are declared once in :data:`SETTINGS` and resolved in
last-one-wins order:

    built-in defaults -> environment variables -> config file -> CLI options

The config file is TOML, either named explicitly or found at
``$XDG_CONFIG_HOME/org-cli-helper/config.toml``.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from . import xdgpaths


class ConfigError(Exception):
    """Raised for malformed configuration values or files."""


# -- value coercion ---------------------------------------------------------

_DURATION_UNITS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]*)")

_NEVER = frozenset({"never", "none", "inf", "infinite", "forever", "0"})


def parse_duration(value: Any) -> int | None:
    """Coerce a duration to whole seconds; None means "no expiry".

    Accepts an int/float of seconds or a string such as ``"1h"``,
    ``"90m"``, ``"1h30m"``, or one of ``never``/``none``/``inf``/``0``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigError(f"invalid duration: {value!r}")
    if isinstance(value, (int, float)):
        seconds = int(value)
        if seconds < 0:
            raise ConfigError(f"negative duration: {value!r}")
        return seconds or None

    text = str(value).strip().lower()
    if not text:
        raise ConfigError("empty duration")
    if text in _NEVER:
        return None

    # Whitespace is insignificant: "1h30m", "1h 30m" and "2 hours" all count.
    text = re.sub(r"\s+", "", text)

    total = 0.0
    matched = 0
    for match in _DURATION_RE.finditer(text):
        number, unit = match.groups()
        if unit and unit not in _DURATION_UNITS:
            raise ConfigError(f"unknown duration unit {unit!r} in {value!r}")
        total += float(number) * _DURATION_UNITS.get(unit or "s", 1)
        matched += len(match.group(0))
    if matched != len(text) or not matched:
        raise ConfigError(f"invalid duration: {value!r}")
    return int(total) or None


def parse_path(value: Any) -> Path | None:
    """Expand ``~`` and environment variables into an absolute Path."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Path(os.path.expandvars(text)).expanduser()


def parse_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    raise ConfigError(f"invalid boolean: {value!r}")


# -- declared settings ------------------------------------------------------


@dataclass(frozen=True)
class Setting:
    """One configurable value, declared once and used by every layer."""

    name: str  # attribute on Config
    section: str  # TOML table
    key: str  # TOML key within the table
    env: str  # environment variable
    default: Any
    coerce: Callable[[Any], Any]
    help: str


SETTINGS: tuple[Setting, ...] = (
    Setting(
        "server_name",
        "daemon",
        "server_name",
        "ORG_CLI_HELPER_SERVER_NAME",
        "org-cli-helper",
        parse_str,
        "Emacs server name used for the managed daemon.",
    ),
    Setting(
        "lifetime",
        "daemon",
        "lifetime",
        "ORG_CLI_HELPER_LIFETIME",
        "1h",
        parse_duration,
        "Idle time before the daemon exits; 'never' disables expiry.",
    ),
    Setting(
        "emacs",
        "daemon",
        "emacs",
        "ORG_CLI_HELPER_EMACS",
        "emacs",
        parse_str,
        "The emacs executable used to start the daemon.",
    ),
    Setting(
        "emacsclient",
        "daemon",
        "emacsclient",
        "ORG_CLI_HELPER_EMACSCLIENT",
        "emacsclient",
        parse_str,
        "The emacsclient executable used to talk to the daemon.",
    ),
    Setting(
        "start_timeout",
        "daemon",
        "start_timeout",
        "ORG_CLI_HELPER_START_TIMEOUT",
        "120s",
        parse_duration,
        "How long to wait for a starting daemon to become ready.",
    ),
    Setting(
        "roam_directory",
        "roam",
        "directory",
        "ORG_CLI_HELPER_ROAM_DIRECTORY",
        None,
        parse_path,
        "org-roam-directory; discovered from the daemon when unset.",
    ),
    Setting(
        "dailies_directory",
        "roam",
        "dailies_directory",
        "ORG_CLI_HELPER_DAILIES_DIRECTORY",
        None,
        parse_str,
        "org-roam-dailies-directory; discovered from the daemon when unset.",
    ),
    Setting(
        "note_topic",
        "note",
        "topic",
        "ORG_CLI_HELPER_NOTE_TOPIC",
        "Context Notes",
        parse_str,
        "org-roam topic 'note' captures into; -T/--topic overrides it.",
    ),
    Setting(
        "pager",
        "log",
        "pager",
        "ORG_CLI_HELPER_PAGER",
        None,
        parse_str,
        "Pager for 'log' output; falls back to $PAGER then less/more.",
    ),
    Setting(
        "store_backend",
        "store",
        "backend",
        "ORG_CLI_HELPER_STORE_BACKEND",
        "sqlite",
        parse_str,
        "Which store backend holds org content.",
    ),
    Setting(
        "store_database",
        "store",
        "database",
        "ORG_CLI_HELPER_STORE_DATABASE",
        None,
        parse_path,
        "Sqlite file for the 'sqlite' store; defaults under the XDG state dir.",
    ),
    Setting(
        "store_directory",
        "store",
        "directory",
        "ORG_CLI_HELPER_STORE_DIRECTORY",
        None,
        parse_path,
        "Org file directory for the 'orgfs' store; defaults under XDG state.",
    ),
)

_BY_NAME = {setting.name: setting for setting in SETTINGS}


@dataclass(frozen=True)
class Config:
    """Fully resolved configuration."""

    server_name: str = "org-cli-helper"
    lifetime: int | None = 3600
    emacs: str = "emacs"
    emacsclient: str = "emacsclient"
    start_timeout: int | None = 120
    roam_directory: Path | None = None
    dailies_directory: str | None = None
    note_topic: str = "Context Notes"
    pager: str | None = None
    store_backend: str = "sqlite"
    store_database: Path | None = None
    store_directory: Path | None = None
    source: Path | None = None  # config file actually loaded, if any

    def describe(self) -> list[tuple[str, str, Any]]:
        """Return ``(dotted_key, env_var, value)`` for each setting."""
        return [
            (f"{s.section}.{s.key}", s.env, getattr(self, s.name))
            for s in SETTINGS
        ]


def _from_environ(environ: dict[str, str]) -> dict[str, Any]:
    return {
        s.name: environ[s.env] for s in SETTINGS if environ.get(s.env, "") != ""
    }


def _from_file(data: dict[str, Any], path: Path) -> dict[str, Any]:
    known: dict[str, set[str]] = {}
    for s in SETTINGS:
        known.setdefault(s.section, set()).add(s.key)

    values: dict[str, Any] = {}
    for section, contents in data.items():
        if section not in known:
            raise ConfigError(f"{path}: unknown section [{section}]")
        if not isinstance(contents, dict):
            raise ConfigError(f"{path}: [{section}] must be a table")
        for key, value in contents.items():
            if key not in known[section]:
                raise ConfigError(f"{path}: unknown key {section}.{key}")
            setting = next(
                s for s in SETTINGS if s.section == section and s.key == key
            )
            values[setting.name] = value
    return values


def read_file(path: Path) -> dict[str, Any]:
    """Parse a TOML config file into raw setting values."""
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: {exc.strerror}") from exc
    return _from_file(data, path)


def load(
    path: Path | None = None,
    overrides: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
) -> Config:
    """Resolve configuration from all layers.

    ``path`` names an explicit config file; when None the XDG location is
    used if it exists.  ``overrides`` are command line values and win over
    everything; entries whose value is None are ignored so that unset
    Click options do not clobber lower layers.
    """
    environ = os.environ if environ is None else environ

    source: Path | None = None
    if path is not None:
        if not path.exists():
            raise ConfigError(f"{path}: no such config file")
        source = path
    else:
        candidate = xdgpaths.config_file()
        if candidate.exists():
            source = candidate

    raw: dict[str, Any] = {}
    raw.update(_from_environ(dict(environ)))
    if source is not None:
        raw.update(read_file(source))
    if overrides:
        raw.update({k: v for k, v in overrides.items() if v is not None})

    resolved: dict[str, Any] = {}
    for name, value in raw.items():
        setting = _BY_NAME[name]
        try:
            resolved[name] = setting.coerce(value)
        except ConfigError as exc:
            raise ConfigError(f"{setting.section}.{setting.key}: {exc}") from exc

    # Defaults for anything no layer supplied.
    for setting in SETTINGS:
        resolved.setdefault(setting.name, setting.coerce(setting.default))

    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in resolved.items() if k in known}, source=source)
