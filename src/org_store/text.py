# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reading the text out of a document.

What counts as the text of a node is a fact about the model, not about
storage or CRUDS, so it lives beside the model and depends on nothing
else.  Both the index and every adapter get the same answer, and the
full-text index can use it without importing the service layer.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .model import OrgDocument, OrgNodeBase


def plain_text(contents: Iterable[Any]) -> str:
    """Flatten contents to their text, dropping markup.

    Three node shapes need care.  A leaf whose text is held verbatim keeps
    it in ``properties.value`` and has no ``contents`` at all: source
    blocks, examples, code, and LaTeX all work this way, and reading only
    ``contents`` silently loses them.  Nodes whose children are discrete
    -- table rows and cells, list items -- are spaced apart rather than
    run together.  And org-element moves the whitespace that followed a
    node out of the tree into its ``post-blank`` count, so a flattener
    that ignores it fuses the last word of one node onto the first of the
    next.

    This is not a faithful re-rendering of the source: ``post-blank``
    counts blank lines for block elements and spaces for inline ones, and
    both are emitted as spaces here.  Reproducing org text is the
    renderer's job; this only has to yield the words, in order, separated.
    """
    out: list[str] = []
    previous_was_node = False
    for item in contents:
        if isinstance(item, str):
            out.append(item)
            previous_was_node = False
            continue
        if not isinstance(item, OrgNodeBase):
            continue
        text = node_text(item)
        # Two nodes side by side with no text between them are separate
        # things -- a keyword and the paragraph under it, an item and the
        # list nested in it -- and their words must not fuse.  Only
        # between *nodes*: a node followed by literal text already has
        # whatever spacing the document gave it, and inserting more would
        # put a space before the comma in "*bold*, ".
        if previous_was_node and _touching(out, text):
            out.append(" ")
        out.append(text)
        out.append(" " * _post_blank(item))
        previous_was_node = True
    return "".join(out)


def _touching(out: list[str], text: str) -> bool:
    """True when appending TEXT would run two words together."""
    if not text or text[:1].isspace():
        return False
    previous = "".join(out)
    return bool(previous) and not previous[-1:].isspace()


def node_text(node: OrgNodeBase) -> str:
    """The text of one node, including any held verbatim."""
    if not node.contents:
        value = node.properties.get("value")
        return value if isinstance(value, str) else ""
    if node.separates_children:
        parts = (plain_text([child]).strip() for child in node.contents)
        return " ".join(part for part in parts if part)
    return plain_text(node.contents)


def own_text(node: OrgNodeBase) -> str:
    """The text this node holds itself, not counting its children's.

    Full-text indexing gives every node a row, so each word must belong
    to exactly one of them.  :func:`node_text` cannot do that -- it
    flattens the whole subtree, so a word under a headline would also be
    indexed against its section, its paragraph, and the headline, and one
    search would return four hits on the same sentence.

    So a node contributes only the strings sitting directly in its
    ``contents``, plus a verbatim ``value`` when it has no contents at all
    (source blocks, LaTeX, examples).  Every word in the document lands in
    exactly one row, and no type-by-type knowledge is needed to decide
    which.

    The cost is that a phrase running through markup -- ``very *loud*
    noise`` -- is split across two rows, so it matches as words but not as
    a phrase.  Emphasis mid-phrase is rare enough to accept that.

    Each child that is left out still leaves a space behind.  Without it
    the text either side of a footnote reference or a LaTeX fragment would
    close up into one word -- ``reference`` and ``in`` becoming
    ``referencein``, which is a token that matches neither.
    """
    direct = "".join(
        item if isinstance(item, str) else " " for item in node.contents
    )
    if direct.strip():
        return direct
    value = node.properties.get("value")
    return value if isinstance(value, str) else ""


def document_title(doc: OrgDocument) -> str:
    """The document's ``#+title`` as a single line."""
    return plain_text(doc.properties.title)


def heading_text(node: OrgNodeBase) -> str:
    """A headline's own title, without the text of anything beneath it."""
    return plain_text(node.properties.get("title") or ())


def _post_blank(node: OrgNodeBase) -> int:
    """How much whitespace org-element took out from after this node."""
    count = node.properties.get("post_blank")
    return count if isinstance(count, int) and count > 0 else 0
