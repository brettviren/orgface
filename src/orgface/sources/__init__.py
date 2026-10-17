# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Sources: where ``slurp`` gets data from and ``orger`` makes org from.

A *source* has a name and a kind.  The ``slurp`` kind fetches data from
somewhere outside and keeps it locally in a durable form, typically
dated JSON files.  The ``orger`` kind turns data that is already on this
computer, such as a slurp's files, into org content.  One name, such as
``github``, usually has both.

A source is a :class:`Source`.  The ones bundled with orgface are the
subpackages of this package, which expose a ``SLURP`` and/or an ``ORGER``.
A source distributed on its own registers the same object under an entry
point group, in its ``pyproject.toml``::

    [project.entry-points."orgface.slurp"]
    inspirehep = "orgface_inspirehep:SLURP"

    [project.entry-points."orgface.orger"]
    inspirehep = "orgface_inspirehep:ORGER"

Either way the command appears under ``orgface slurp`` or ``orgface
orger``, and its settings under ``[slurp.NAME]`` or ``[orger.NAME]`` in
the config file.

A bundled source names its optional dependencies in ``pyproject.toml``
under an extra of the same name as the source, so ``uv tool install -e
'.[github]'`` provides what ``github`` needs.

Importing a source's module must not need what the source depends on:
the listing has to work, and say what is missing, when it is missing.
Import the dependencies inside the commands instead.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any, Literal

import click

from ..config import Setting

log = logging.getLogger(__name__)

Kind = Literal["slurp", "orger"]

#: The package that is the bundled origin of sources.
BUNDLED = "orgface"


def setting(
    kind: Kind,
    source: str,
    key: str,
    default: Any,
    coerce: Callable[[Any], Any],
    help: str,
    *,
    secret: bool = False,
) -> Setting:
    """Declare one setting of a source.

    The table is ``[KIND.SOURCE]`` and the environment variable is
    ``ORGFACE_KIND_SOURCE_KEY``, so a source never has to spell either and
    every source names them the same way.
    """
    env = f"ORGFACE_{kind}_{source}_{key}".upper().replace("-", "_")
    return Setting(key, f"{kind}.{source}", key, env, default, coerce, help, secret)


@dataclass(frozen=True)
class Source:
    """One source of one kind."""

    kind: Kind
    name: str
    summary: str
    #: The command that runs the source: a group when it has several.
    command: click.Command
    #: Everything configurable about it, as built by :func:`setting`.
    settings: tuple[Setting, ...] = ()
    #: Top-level modules that must be importable for the source to work.
    requires: tuple[str, ...] = ()
    #: The pyproject extra providing them; the source's name when not set.
    extra: str | None = None
    #: The distribution that supplies it; set when discovered.
    origin: str = field(default=BUNDLED, compare=False)

    @property
    def section(self) -> str:
        """The name of its table in the config file."""
        return f"{self.kind}.{self.name}"

    def missing(self) -> tuple[str, ...]:
        """The required modules that cannot be imported."""
        return tuple(name for name in self.requires if not _importable(name))

    @property
    def installed(self) -> bool:
        return not self.missing()

    def install_hint(self) -> str:
        """How to get what is missing."""
        if self.origin != BUNDLED:
            return f"install {', '.join(self.missing())}"
        extra = self.extra or self.name
        return f"install the extra: uv tool install -e '.[{extra}]'"


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _bundled(kind: Kind) -> dict[str, Source]:
    found: dict[str, Source] = {}
    for info in pkgutil.iter_modules(__path__):
        module = importlib.import_module(f"{__name__}.{info.name}")
        source = getattr(module, kind.upper(), None)
        if isinstance(source, Source) and source.kind == kind:
            found[source.name] = source
    return found


def _external(kind: Kind) -> dict[str, Source]:
    found: dict[str, Source] = {}
    for point in metadata.entry_points(group=f"orgface.{kind}"):
        origin = point.dist.name if point.dist else "?"
        try:
            source = point.load()
        except Exception as exc:  # noqa: BLE001 -- a broken plugin must not break the others
            log.warning("%s source %s from %s: %s", kind, point.name, origin, exc)
            continue
        if not isinstance(source, Source) or source.kind != kind:
            log.warning("%s source %s from %s is not a %s Source", kind, point.name, origin, kind)
            continue
        if source.name != point.name:
            log.warning(
                "%s source %s from %s calls itself %s; skipped",
                kind, point.name, origin, source.name,
            )
            continue
        found[point.name] = _with_origin(source, origin)
    return found


def _with_origin(source: Source, origin: str) -> Source:
    from dataclasses import replace

    return replace(source, origin=origin)


def registry(kind: Kind) -> dict[str, Source]:
    """Every source of ``kind``, by name; the bundled ones win a clash."""
    found = _external(kind)
    for name in found.keys() & _bundled(kind).keys():
        log.warning("%s source %s from %s is shadowed by the bundled one", kind, name, found[name].origin)
    found.update(_bundled(kind))
    return dict(sorted(found.items()))
