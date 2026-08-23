# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Rendering a document by running org-export inside a real Emacs.

The exporter is org's own, so what comes out is what the person would
get from ``C-c C-e`` -- their backends, their filters, their in-buffer
settings.  Nothing here reimplements any of that; the whole method is:
put the document where Emacs can see it, ask, and take the text back.

A document that is *already* a file is exported where it lies, rather
than through a copy, so that relative links, includes and images resolve
the way they do for a person opening it.  One that lives in a database
has no such place, so it is written to a temporary file first -- which
is the only reason this needs the org renderer at all.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..model import OrgDocument


@runtime_checkable
class OrgExportEngine(Protocol):
    """What this method needs from an Emacs.

    A port rather than an import: :mod:`org_store` is a library the CLI
    uses, not the other way round, so it says what it needs and the CLI
    supplies something that does it.
    """

    def export(self, path: Path, backend: str) -> str:
        """The org file at PATH, exported by the named org-export backend."""
        ...


def render(
    doc: OrgDocument,
    *,
    fmt: str = "html",
    emacs: OrgExportEngine | None = None,
    doc_id: str = "",
    source: Path | None = None,
    **_: Any,
) -> str:
    """Export DOC as FMT through EMACS."""
    from . import ExportError, get_format

    if emacs is None:
        raise ExportError("the 'emacs' render method needs an Emacs to run in")
    exporter = get_format(fmt).exporter

    if source is not None and Path(source).is_file():
        return emacs.export(Path(source), exporter)

    with tempfile.TemporaryDirectory(prefix="org-cli-helper-") as folder:
        path = Path(folder) / (Path(doc_id).name or "document")
        path = path.with_suffix(".org")
        path.write_text(_org_text(doc), encoding="utf-8")
        return emacs.export(path, exporter)


def _org_text(doc: OrgDocument) -> str:
    from ..render import render_document

    text = render_document(doc)
    return text if text.endswith("\n") else text + "\n"
