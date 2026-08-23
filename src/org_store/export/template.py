# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Rendering a document by applying a Jinja2 template to its JSON.

The template is handed the canonical document as plain data -- the same
JSON ``org store show --json`` prints -- and walks it itself.  That is
the point of this method: somebody who wants a different HTML, or a
format nobody has thought of, writes a template rather than Python.

The template does the escaping.  Autoescaping is off because a template
here *is* writing markup, and turning it on would escape the markup along
with the text; every packaged template escapes what came from the
document with ``|e`` and nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..model import OrgDocument
from ..text import document_title

TEMPLATES = Path(__file__).parent / "templates"


def render(
    doc: OrgDocument,
    *,
    fmt: str = "html",
    template: Path | None = None,
    doc_id: str = "",
    **_: Any,
) -> str:
    """Render DOC with TEMPLATE, or with the one FMT names."""
    from . import ExportError, get_format

    chosen = Path(template) if template else TEMPLATES / get_format(fmt).template
    try:
        text = chosen.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExportError(f"{chosen}: {exc.strerror or exc}") from exc

    environment = _environment(chosen.parent)
    return environment.from_string(text).render(context(doc, doc_id, fmt))


def context(doc: OrgDocument, doc_id: str = "", fmt: str = "") -> dict[str, Any]:
    """What a template may use.

    The document is passed as data rather than as the model, so a
    template cannot depend on anything but the schema -- which is the
    published thing, and the one that will not move under it.
    """
    from . import FORMATS

    return {
        "document": doc.dump(),
        "doc_id": doc_id,
        "title": document_title(doc) or doc_id,
        "filetags": list(doc.properties.filetags),
        # What a file of this rendering is called, so a template can point
        # a link at a sibling's rendering rather than at its source.
        "extension": FORMATS[fmt].extension if fmt in FORMATS else "",
    }


def _environment(directory: Path):
    """A Jinja2 environment loading from DIRECTORY.

    Loading from a directory rather than only from the named file is what
    lets a template of somebody's own ``{% include %}`` or ``{% extends %}``
    another beside it.
    """
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    return Environment(
        loader=FileSystemLoader([str(directory), str(TEMPLATES)]),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
