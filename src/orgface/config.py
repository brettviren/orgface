# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Layered configuration for orgface.

Settings are declared once in :data:`SETTINGS` and resolved in
last-one-wins order:

    built-in defaults -> environment variables -> config file
        -> host file -> CLI options

The config file is TOML, either named explicitly or found at
``$XDG_CONFIG_HOME/orgface/config.toml``.  Beside it, an optional
``<host>.toml`` named for this machine's short host name overrides it, so
one directory shared between machines can hold every machine's settings.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from . import xdgpaths

log = logging.getLogger(__name__)


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


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"invalid whole number: {value!r}") from exc


def parse_count(value: Any) -> int | None:
    """A whole number of at least one."""
    number = parse_int(value)
    if number is not None and number < 1:
        raise ConfigError(f"must be at least 1: {value!r}")
    return number


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
    secret: bool = False  # withheld from displayed configuration


SETTINGS: tuple[Setting, ...] = (
    Setting(
        "server_name",
        "daemon",
        "server_name",
        "ORGFACE_SERVER_NAME",
        "orgface",
        parse_str,
        "Emacs server name used for the managed daemon.",
    ),
    Setting(
        "lifetime",
        "daemon",
        "lifetime",
        "ORGFACE_LIFETIME",
        "1h",
        parse_duration,
        "Idle time before the daemon exits; 'never' disables expiry.",
    ),
    Setting(
        "emacs",
        "daemon",
        "emacs",
        "ORGFACE_EMACS",
        "emacs",
        parse_str,
        "The emacs executable used to start the daemon.",
    ),
    Setting(
        "emacsclient",
        "daemon",
        "emacsclient",
        "ORGFACE_EMACSCLIENT",
        "emacsclient",
        parse_str,
        "The emacsclient executable used to talk to the daemon.",
    ),
    Setting(
        "start_timeout",
        "daemon",
        "start_timeout",
        "ORGFACE_START_TIMEOUT",
        "120s",
        parse_duration,
        "How long to wait for a starting daemon to become ready.",
    ),
    Setting(
        "roam_directory",
        "roam",
        "directory",
        "ORGFACE_ROAM_DIRECTORY",
        None,
        parse_path,
        "org-roam-directory; discovered from the daemon when unset.",
    ),
    Setting(
        "dailies_directory",
        "roam",
        "dailies_directory",
        "ORGFACE_DAILIES_DIRECTORY",
        None,
        parse_str,
        "org-roam-dailies-directory; discovered from the daemon when unset.",
    ),
    Setting(
        "note_topic",
        "note",
        "topic",
        "ORGFACE_NOTE_TOPIC",
        "Context Notes",
        parse_str,
        "org-roam topic 'note' captures into; -T/--topic overrides it.",
    ),
    Setting(
        "pager",
        "log",
        "pager",
        "ORGFACE_PAGER",
        None,
        parse_str,
        "Pager for 'log' output; falls back to $PAGER then less/more.",
    ),
    Setting(
        "store_backend",
        "store",
        "backend",
        "ORGFACE_STORE_BACKEND",
        "sqlite",
        parse_str,
        "Which store backend holds org content.",
    ),
    Setting(
        "store_database",
        "store",
        "database",
        "ORGFACE_STORE_DATABASE",
        None,
        parse_path,
        "Sqlite file for the 'sqlite' store; defaults under the XDG state dir.",
    ),
    Setting(
        "store_directory",
        "store",
        "directory",
        "ORGFACE_STORE_DIRECTORY",
        None,
        parse_path,
        "Org file directory for the 'orgfs' store; defaults under XDG state.",
    ),
)

_BY_NAME = {setting.name: setting for setting in SETTINGS}

#: The section that may repeat, as ``[[embed]]``, one table per service.
EMBED = "embed"

#: Settings of one embedding service.  ``name`` is the attribute on
#: :class:`EmbedService`; the environment variables describe one service,
#: and supply what a service in a file leaves out.
EMBED_SETTINGS: tuple[Setting, ...] = (
    Setting(
        "url",
        "embed",
        "url",
        "ORGFACE_EMBED_URL",
        "http://127.0.0.1:8080",
        parse_str,
        "Base URL of an OpenAI-compatible embeddings service (with or without /v1).",
    ),
    Setting(
        "model",
        "embed",
        "model",
        "ORGFACE_EMBED_MODEL",
        "nomic-embed-text-v1.5",
        parse_str,
        "What this service calls the embedding model; sent with each request.",
    ),
    Setting(
        "dimensions",
        "embed",
        "dimensions",
        "ORGFACE_EMBED_DIMENSIONS",
        768,
        parse_int,
        "How many numbers the embedding model returns per vector.",
    ),
    Setting(
        "api_key",
        "embed",
        "api_key",
        "ORGFACE_EMBED_API_KEY",
        None,
        parse_str,
        "Bearer token for the embeddings service; prefer the environment variable.",
        secret=True,
    ),
    Setting(
        "api_key_passname",
        "embed",
        "api_key_passname",
        "ORGFACE_EMBED_API_KEY_PASSNAME",
        None,
        parse_str,
        "pass(1) entry whose first line is the API key, used when api_key is unset.",
    ),
    Setting(
        "identity",
        "embed",
        "identity",
        "ORGFACE_EMBED_IDENTITY",
        None,
        parse_str,
        "Which model this is, whatever the service calls it; vectors are kept"
        " per identity. Defaults to model.",
    ),
    Setting(
        "batch",
        "embed",
        "batch",
        "ORGFACE_EMBED_BATCH",
        32,
        parse_count,
        "Texts sent in one request; lowered, with a warning, if the service"
        " refuses that many.",
    ),
    Setting(
        "parallel",
        "embed",
        "parallel",
        "ORGFACE_EMBED_PARALLEL",
        1,
        parse_count,
        "Requests in flight at once; lowered, with a warning, if the service"
        " cannot keep up.",
    ),
)

_EMBED_BY_KEY = {setting.key: setting for setting in EMBED_SETTINGS}


@dataclass(frozen=True)
class EmbedService:
    """One embedding service: where it is, and which model it serves."""

    url: str = "http://127.0.0.1:8080"
    model: str = "nomic-embed-text-v1.5"
    dimensions: int = 768
    api_key: str | None = None
    api_key_passname: str | None = None
    identity: str = "nomic-embed-text-v1.5"
    batch: int = 32
    parallel: int = 1

    def values(self) -> dict[str, Any]:
        """Every setting's value, keyed by setting name."""
        return {s.name: getattr(self, s.name) for s in EMBED_SETTINGS}


@dataclass(frozen=True)
class Config:
    """Fully resolved configuration."""

    server_name: str = "orgface"
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
    embed: tuple[EmbedService, ...] = (EmbedService(),)  # in the order to try
    sources: tuple[Path, ...] = ()  # config files actually loaded, in order
    #: Raw ``[slurp.NAME]`` and ``[orger.NAME]`` tables keyed by "slurp.NAME".
    #: A source declares its own settings, so these are only checked and
    #: coerced when the source is used; see :func:`resolve_section`.
    source_tables: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def values(self) -> dict[str, Any]:
        """Return every setting's resolved value, keyed by setting name.

        The embedding services are under ``"embed"``, as a list of their
        own values in the same form.
        """
        values = {s.name: getattr(self, s.name) for s in SETTINGS}
        values[EMBED] = [service.values() for service in self.embed]
        return values


def _toml_value(setting: Setting, value: Any) -> str | None:
    """Render one value as a TOML literal; None means "leave it unset"."""
    if value is None:
        # An absent duration means "no expiry", which must be said
        # explicitly or the file would fall back to the default.
        return '"never"' if setting.coerce is parse_duration else None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    # JSON string escapes are a subset of TOML basic-string escapes.
    return json.dumps(str(value), ensure_ascii=False)


def to_toml(
    values: dict[str, Any],
    header: list[str] | None = None,
    env: bool = False,
    comments: bool = False,
    secrets: bool = False,
) -> str:
    """Render setting values, keyed by setting name, as a TOML document.

    Settings are grouped into one table per section, in declaration
    order, followed by one ``[[embed]]`` table per embedding service.
    Unset values are written as commented-out keys so the file still
    names every setting.  ``env`` appends each environment variable as a
    trailing comment; ``comments`` precedes each key with its help text.
    Secret settings are written commented out, value withheld, unless
    ``secrets`` is true.  The result always parses back with
    :func:`read_file`.
    """
    sections: dict[str, list[Setting]] = {}
    for setting in SETTINGS:
        sections.setdefault(setting.section, []).append(setting)

    tables = [(f"[{section}]", settings, values) for section, settings in sections.items()]
    tables += [
        (f"[[{EMBED}]]", EMBED_SETTINGS, service)
        for service in values.get(EMBED, [])
    ]

    lines = [f"# {text}" for text in header or []]
    for title, settings, table in tables:
        if lines:
            lines.append("")
        lines += table_lines(
            title, settings, table, env=env, comments=comments, secrets=secrets
        )
    return "\n".join(lines) + "\n"


def table_lines(
    title: str,
    settings: tuple[Setting, ...],
    table: dict[str, Any],
    env: bool = False,
    comments: bool = False,
    secrets: bool = False,
) -> list[str]:
    """Render one TOML table, as :func:`to_toml` does for each of its own."""
    lines = [title]
    for setting in settings:
        if comments:
            lines.append(f"# {setting.help}")
        literal = _toml_value(setting, table.get(setting.name))
        if literal is None:
            line = f"# {setting.key} ="
        elif setting.secret and not secrets:
            line = f"# {setting.key} = (set, withheld)"
        else:
            line = f"{setting.key} = {literal}"
        if env:
            line = f"{line:<40} # {setting.env}"
        lines.append(line)
    return lines


def defaults() -> dict[str, Any]:
    """The built-in default of every setting, keyed by setting name."""
    values = {s.name: s.default for s in SETTINGS}
    values[EMBED] = [{s.name: s.default for s in EMBED_SETTINGS}]
    return values


def init_file(path: Path) -> bool:
    """Write a default config file at ``path`` unless one exists.

    Returns True when the file was created, False when it already existed.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    text = to_toml(
        defaults(),
        header=["orgface configuration", "Settings left commented out are unset."],
        comments=True,
    )
    path.write_text(text, encoding="utf-8")
    return True


def _from_environ(
    environ: dict[str, str], settings: tuple[Setting, ...] = SETTINGS
) -> dict[str, Any]:
    return {
        s.name: environ[s.env] for s in settings if environ.get(s.env, "") != ""
    }


def _embed_table(contents: Any, path: Path) -> dict[str, Any]:
    """One ``[[embed]]`` table's raw values, keyed by setting name."""
    if not isinstance(contents, dict):
        raise ConfigError(f"{path}: [[{EMBED}]] must be a table")
    values: dict[str, Any] = {}
    for key, value in contents.items():
        if key not in _EMBED_BY_KEY:
            raise ConfigError(f"{path}: unknown key {EMBED}.{key}")
        values[_EMBED_BY_KEY[key].name] = value
    return values


#: Top-level tables holding one sub-table per source: ``[slurp.github]``.
SOURCE_KINDS = ("slurp", "orger")

#: Where :func:`_from_file` keeps the source tables among the setting values.
SOURCE_TABLES = "source_tables"


def _source_tables(kind: str, contents: Any, path: Path) -> dict[str, dict[str, Any]]:
    """The tables of one kind, keyed by their dotted name."""
    if not isinstance(contents, dict):
        raise ConfigError(f"{path}: [{kind}] must be a table")
    tables: dict[str, dict[str, Any]] = {}
    for name, table in contents.items():
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: [{kind}.{name}] must be a table")
        tables[f"{kind}.{name}"] = dict(table)
    return tables


def _from_file(data: dict[str, Any], path: Path) -> dict[str, Any]:
    """Raw setting values, keyed by setting name.

    The embedding services, if the file names any, are under ``"embed"``
    as a list.  A single ``[embed]`` table counts as a list of one.
    """
    known: dict[str, set[str]] = {}
    for s in SETTINGS:
        known.setdefault(s.section, set()).add(s.key)

    values: dict[str, Any] = {}
    for section, contents in data.items():
        if section in SOURCE_KINDS:
            values[SOURCE_TABLES] = {
                **values.get(SOURCE_TABLES, {}),
                **_source_tables(section, contents, path),
            }
            continue
        if section == EMBED:
            tables = contents if isinstance(contents, list) else [contents]
            values[EMBED] = [_embed_table(table, path) for table in tables]
            continue
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


def _coerce(setting: Setting, value: Any, where: str) -> Any:
    try:
        return setting.coerce(value)
    except ConfigError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def _embed_service(raw: dict[str, Any], under: dict[str, Any], index: int) -> EmbedService:
    """Resolve one service: its own values over those it inherits."""
    merged = {**under, **raw}
    resolved = {
        s.name: _coerce(s, merged.get(s.name, s.default), f"{EMBED}[{index}].{s.key}")
        for s in EMBED_SETTINGS
    }
    resolved["identity"] = resolved["identity"] or resolved["model"]
    return EmbedService(**resolved)


#: Names this machine for choosing its host file, instead of its host name.
HOST_ENV = "ORGFACE_HOST"


def host_name(environ: dict[str, str] | None = None) -> str:
    """This machine's short host name, as ``hostname -s`` prints it."""
    environ = os.environ if environ is None else environ
    return environ.get(HOST_ENV) or socket.gethostname().split(".")[0]


def host_file(base: Path, host: str) -> Path:
    """The host file that goes with the config file BASE."""
    return base.parent / f"{host}.toml"


def load(
    path: Path | None = None,
    overrides: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
) -> Config:
    """Resolve configuration from all layers.

    ``path`` names an explicit config file; when None the XDG location is
    used if it exists.  Either way a host file beside it, named for this
    machine, is read after it when present.  ``overrides`` are command
    line values and win over everything; entries whose value is None are
    ignored so that unset Click options do not clobber lower layers.
    """
    environ = os.environ if environ is None else environ

    base = path if path is not None else xdgpaths.config_file()
    if path is not None and not path.exists():
        raise ConfigError(f"{path}: no such config file")
    host = host_file(base, host_name(environ))
    log.debug("looking for config at %s and %s", base, host)
    sources = tuple(
        candidate
        for candidate in dict.fromkeys((base, host))
        if candidate.exists()
    )

    raw: dict[str, Any] = {}
    raw.update(_from_environ(dict(environ)))
    source_tables: dict[str, dict[str, Any]] = {}
    for source in sources:
        values = read_file(source)
        # Unlike the rest, a source's table merges key by key across the
        # files, so a host file can change one value and inherit the rest.
        for name, table in values.pop(SOURCE_TABLES, {}).items():
            source_tables.setdefault(name, {}).update(table)
        raw.update(values)
    if overrides:
        raw.update({k: v for k, v in overrides.items() if v is not None})

    # The services as a whole come from the last layer naming any, so a
    # host file lists the services that host uses rather than adding to
    # another host's.  The environment fills in what a service leaves out.
    under = _from_environ(dict(environ), EMBED_SETTINGS)
    services = raw.pop(EMBED, None) or [{}]
    log.debug("%d embedding service(s) configured", len(services))
    embed = tuple(
        _embed_service(service, under, index)
        for index, service in enumerate(services)
    )

    resolved: dict[str, Any] = {}
    for name, value in raw.items():
        setting = _BY_NAME[name]
        resolved[name] = _coerce(setting, value, f"{setting.section}.{setting.key}")

    # Defaults for anything no layer supplied.
    for setting in SETTINGS:
        resolved.setdefault(setting.name, setting.coerce(setting.default))

    known = {f.name for f in fields(Config)}
    return Config(
        **{k: v for k, v in resolved.items() if k in known},
        embed=embed,
        sources=sources,
        source_tables=source_tables,
    )


def check_section(config: Config, section: str, settings: tuple[Setting, ...]) -> None:
    """Raise :class:`ConfigError` for a key in the table that is not a setting.

    A misspelt key, or a right one in the wrong table, would otherwise be
    ignored without a word.
    """
    known = {setting.key for setting in settings}
    for key in config.source_tables.get(section, {}):
        if key not in known:
            raise ConfigError(
                f"[{section}]: unknown key {key!r}; known keys: "
                f"{', '.join(sorted(known)) or 'none'}"
            )


def resolve_section(
    config: Config,
    section: str,
    settings: tuple[Setting, ...],
    overrides: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve the settings of one source, keyed by setting name.

    ``section`` is the dotted table name, e.g. ``"slurp.github"``.  The
    layers are the same as for the rest of the configuration: defaults,
    then environment variables, then the config files, then ``overrides``
    (command line values; those that are None are ignored).  A key in the
    file that the source does not declare is an error, which is how a
    misspelt setting gets noticed.
    """
    environ = os.environ if environ is None else environ
    check_section(config, section, settings)
    by_key = {setting.key: setting for setting in settings}
    table = config.source_tables.get(section, {})

    raw: dict[str, Any] = _from_environ(dict(environ), settings)
    raw.update({by_key[key].name: value for key, value in table.items()})
    if overrides:
        raw.update({k: v for k, v in overrides.items() if v is not None})
    return {
        setting.name: _coerce(
            setting, raw.get(setting.name, setting.default), f"{section}.{setting.key}"
        )
        for setting in settings
    }
