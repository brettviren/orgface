# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Turning a stored document into some other kind of text.

Distinct from :mod:`org_store.render`, which writes the canonical model
back out as *org* text and is how the file store saves a document.  This
is the other direction a person asks for: give me this document as HTML.

Two methods, declared here rather than known to any caller:

template
    A Jinja2 template applied to the document's canonical JSON.  Needs
    nothing but the template, so it works wherever the store does.

emacs
    org-export, run inside a real Emacs.  Slower, and it needs a daemon,
    but the exporter is org's own -- with whatever backends, filters and
    settings the person's configuration provides.

A format says what is being produced: which packaged template renders it,
which org-export backend produces it, and what a file of it is called.
Adding one is an entry in :data:`FORMATS` and a template beside it, not
an edit to any method.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..model import OrgDocument


class ExportError(Exception):
    """A document could not be rendered as asked."""


@dataclass(frozen=True)
class Format:
    """One kind of text a document can be turned into."""

    name: str
    #: What a file of it is called.
    extension: str
    #: The packaged template that renders it, under ``templates/``.
    template: str
    #: The org-export backend that produces it inside Emacs.
    exporter: str
    summary: str


@dataclass(frozen=True)
class Method:
    """One way of producing that text."""

    name: str
    render: Callable[..., str]
    summary: str
    #: True when it needs an Emacs to render through.  Declared here so
    #: the caller supplying one does not have to know which method that is.
    needs_emacs: bool = False


FORMATS: dict[str, Format] = {
    "html": Format(
        name="html",
        extension=".html",
        template="html.j2",
        exporter="html",
        summary="a standalone HTML page",
    ),
}


def _template(**kwargs: Any) -> str:
    from .template import render as run

    return run(**kwargs)


def _emacs(**kwargs: Any) -> str:
    from .emacs import render as run

    return run(**kwargs)


METHODS: dict[str, Method] = {
    "template": Method(
        name="template",
        render=_template,
        summary="a Jinja2 template applied to the document's JSON",
    ),
    "emacs": Method(
        name="emacs",
        render=_emacs,
        summary="org-export, run inside a real Emacs",
        needs_emacs=True,
    ),
}


def get_format(name: str) -> Format:
    """The named format, or an error naming the real ones."""
    try:
        return FORMATS[name]
    except KeyError:
        known = ", ".join(sorted(FORMATS))
        raise ExportError(f"unknown format {name!r}; known: {known}") from None


def get_method(name: str) -> Method:
    """The named method, or an error naming the real ones."""
    try:
        return METHODS[name]
    except KeyError:
        known = ", ".join(sorted(METHODS))
        raise ExportError(f"unknown render method {name!r}; known: {known}") from None


def render(
    doc: OrgDocument,
    *,
    fmt: str = "html",
    method: str = "template",
    template: Path | None = None,
    emacs: Any = None,
    doc_id: str = "",
    source: Path | None = None,
) -> str:
    """Render DOC as FMT, by METHOD.

    ``template`` names a Jinja2 template to use instead of the format's
    own.  ``source`` is the file the document is stored in, when it is
    stored in one: the Emacs method exports that file rather than a copy
    of it, so that relative links and includes resolve as they would for
    a person opening it.
    """
    chosen = get_method(method)
    get_format(fmt)
    if chosen.needs_emacs and emacs is None:
        raise ExportError(f"the {method!r} render method needs an Emacs to run in")
    return chosen.render(
        doc=doc,
        fmt=fmt,
        template=template,
        emacs=emacs,
        doc_id=doc_id,
        source=source,
    )


__all__ = [
    "FORMATS",
    "METHODS",
    "ExportError",
    "Format",
    "Method",
    "get_format",
    "get_method",
    "render",
]
