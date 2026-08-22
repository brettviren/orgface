# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""What a search asks for, and what it gets back.

Only the shapes live here, plus the one piece of query handling that is
not specific to any index: turning what somebody typed into terms.  How a
given backend answers is its own business, and the service layer decides
which backend can be asked.

Both search modes -- full text now, embeddings later -- return the same
:class:`SearchResult`, so an adapter never has to know which one produced
a row.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from .store import DocId

#: Which text a query is matched against.
Scope = Literal["heading", "body", "all"]


class SearchQuery(BaseModel):
    """One search request."""

    text: str | None = None
    semantic: bool = False
    scope: Scope = "all"
    node_types: list[str] | None = None
    tags: list[str] | None = None
    property_filters: dict[str, str] | None = None
    doc_ids: list[DocId] | None = None
    top_k: int = Field(default=20, ge=1)


class SearchResult(BaseModel):
    """One hit, in the shape every search mode returns."""

    doc_id: DocId
    ref: str
    type: str
    score: float
    excerpt: str = ""


#: A term, optionally ending in ``*`` to match by prefix.
_TERM = re.compile(r"[^\s]+")


def terms(text: str) -> list[str]:
    """Split what somebody typed into FTS5 terms, safely quoted.

    Users type search text, not query syntax.  Passing it through raw
    means ``E=mc^2`` or a stray bracket is a syntax error rather than a
    search, so every term is wrapped as an FTS5 string.  A trailing ``*``
    is kept outside the quotes, which is the one piece of syntax worth
    supporting because prefix search is genuinely useful.
    """
    out: list[str] = []
    for match in _TERM.finditer(text):
        term = match.group(0)
        prefix = term.endswith("*")
        if prefix:
            term = term[:-1]
        if not term:
            continue
        quoted = '"' + term.replace('"', '""') + '"'
        out.append(quoted + "*" if prefix else quoted)
    return out


def match_expression(text: str, scope: Scope = "all") -> str:
    """Build the FTS5 MATCH expression for TEXT within SCOPE.

    Terms are ANDed, which is what someone typing two words expects.
    """
    joined = " ".join(terms(text))
    if not joined:
        return ""
    if scope == "all":
        return joined
    column = "heading_text" if scope == "heading" else "body_text"
    return f"{{{column}}} : ({joined})"
