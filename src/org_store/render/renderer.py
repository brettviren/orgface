# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Dispatching nodes to their templates, and preparing what they need."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any

from ..model import OrgDocument, OrgNodeBase

TEMPLATES = Path(__file__).parent / "templates"

#: Used for any node type without a template of its own.
FALLBACK = "fallback.org.j2"

#: Blocks that are a single line and belong in an unbroken run with their
#: own kind, rather than being spaced apart like paragraphs.  A blank line
#: between two ``#+keyword:`` lines would end the run.
LINE_BLOCKS = frozenset({"keyword", "node-property"})

#: Containers whose children must not be spaced apart at all: a blank line
#: inside a list item or a table ends the construct rather than formats it.
TIGHT = frozenset(
    {"item", "plain-list", "table", "table-row", "property-drawer", "drawer"}
)

#: Node types that occupy whole lines, and so take a blank line between
#: them.  Everything else is inline and is concatenated directly.
BLOCKS = frozenset(
    {
        "center-block",
        "comment-block",
        "drawer",
        "example-block",
        "export-block",
        "fixed-width",
        "footnote-definition",
        "headline",
        "horizontal-rule",
        "keyword",
        "latex-environment",
        "paragraph",
        "plain-list",
        "property-drawer",
        "quote-block",
        "section",
        "special-block",
        "src-block",
        "table",
        "verse-block",
    }
)


class OrgRenderer:
    """Renders canonical documents and nodes as org text."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or TEMPLATES

    @cached_property
    def _env(self):
        from jinja2 import Environment, FileSystemLoader, StrictUndefined

        environment = Environment(
            loader=FileSystemLoader(str(self.directory)),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            autoescape=False,
        )
        environment.filters["oneline"] = _oneline
        environment.filters["body"] = _newline
        return environment

    # -- entry points -------------------------------------------------------

    def document(self, doc: OrgDocument) -> str:
        """Render a whole document.

        A file's keywords are in two places at once: as ``keyword`` nodes
        in the first section, which is where both readers put them, and
        again in ``properties``, which is the convenient view of them.
        Writing both would grow a second ``#+title`` on every save; writing
        only the nodes would silently drop the title of a document built
        in Python, which has properties and no nodes.  So the nodes are
        written, and a property with no node of its own is written too.
        """
        body = _join([(child, self.render(child)) for child in doc.contents])
        missing = _missing_keywords(doc)
        return missing + body if missing else body

    def render(self, node: OrgNodeBase | str) -> str:
        """Render one node, or pass plain text straight through."""
        if isinstance(node, str):
            return node
        template = self._template(node.type)
        return template.render(**self._context(node))

    # -- internals ----------------------------------------------------------

    def _template(self, kind: str):
        from jinja2 import TemplateNotFound

        try:
            return self._env.get_template(f"{kind}.org.j2")
        except TemplateNotFound:
            return self._env.get_template(FALLBACK)

    def _context(self, node: OrgNodeBase) -> dict[str, Any]:
        """Everything a template is allowed to need, worked out here."""
        properties = node.properties
        context: dict[str, Any] = {
            "node": node,
            "props": properties,
            "type": node.type,
            "value": properties.get("value") or "",
            "content": self._contents(node),
        }
        if node.type == "headline":
            context.update(self._headline(node))
        elif node.type == "item":
            context.update(self._item(node))
        elif node.type == "link":
            context["description"] = self._contents(node)
            context["target"] = properties.get("raw_link") or properties.get("path") or ""
        elif node.type == "table-row":
            context["cells"] = [self.render(cell) for cell in node.contents]
            context["rule"] = properties.get("type") == "rule"
        return context

    def _contents(self, node: OrgNodeBase) -> str:
        """The node's children, rendered and joined.

        Inline children run together, because the text between them is
        already in the document as plain strings.  Block children get a
        blank line between them, except inside a TIGHT container, where a
        blank line would break the construct apart.
        """
        rendered = [(child, self.render(child)) for child in node.contents]
        if any(_is_block(child) for child in node.contents):
            return _join(rendered, tight=node.type in TIGHT)
        return "".join(text + _trailing(child) for child, text in rendered)

    def _headline(self, node: OrgNodeBase) -> dict[str, Any]:
        properties = node.properties
        level = properties.get("level") or 1
        heading = ["*" * int(level)]
        if properties.get("todo_keyword"):
            heading.append(str(properties.get("todo_keyword")))
        if properties.get("priority"):
            heading.append(f"[#{properties.get('priority')}]")
        title = "".join(self.render(part) for part in properties.get("title") or ())
        heading.append(_oneline(title) or str(properties.get("raw_value") or ""))

        tags = properties.get("tags") or []
        planning = " ".join(
            f"{name.upper()}: {_stamp(properties.get(name))}"
            for name in ("closed", "scheduled", "deadline")
            if properties.get(name) is not None
        )
        drawer = getattr(node, "drawer", None) or {}
        return {
            "heading": " ".join(part for part in heading if part),
            "tags": ":" + ":".join(tags) + ":" if tags else "",
            "planning": planning,
            "drawer": [(key, str(value)) for key, value in drawer.items()],
        }

    def _item(self, node: OrgNodeBase) -> dict[str, Any]:
        properties = node.properties
        bullet = str(properties.get("bullet") or "- ").rstrip() + " "
        checkbox = {"on": "[X] ", "off": "[ ] ", "trans": "[-] "}.get(
            str(properties.get("checkbox") or ""), ""
        )
        tag = "".join(self.render(part) for part in properties.get("tag") or ())
        return {
            "bullet": bullet,
            "checkbox": checkbox,
            "tag": f"{tag} :: " if tag else "",
            "indent": " " * len(bullet),
        }


_DEFAULT = OrgRenderer()


def render_document(doc: OrgDocument) -> str:
    """Render a whole document with the built-in templates."""
    return _DEFAULT.document(doc)


def render_node(node: OrgNodeBase | str) -> str:
    """Render one node with the built-in templates."""
    return _DEFAULT.render(node)


# -- helpers ----------------------------------------------------------------


#: Document properties written as ``#+keyword:`` lines when no keyword
#: node already carries them.
#:
#: Only the two the store treats as a document's identity, and only these
#: two on purpose.  ox-json fills in ``author``, ``email``, ``language``
#: and ``creator`` from the *exporting Emacs* rather than from anything
#: in the file, so writing those back would put into a file something it
#: never said -- and would make the same document hash differently
#: depending on who read it, which is the one thing the hash must not do.
DOCUMENT_KEYWORDS = (
    ("title", "TITLE"),
    ("filetags", "FILETAGS"),
)


def _missing_keywords(doc: OrgDocument) -> str:
    """Keyword lines for document properties no node already carries."""
    from ..text import plain_text

    present = {
        str(node.properties.get("key") or "").upper()
        for node in _all_nodes(doc.contents)
        if node.type == "keyword"
    }
    lines = []
    for name, keyword in DOCUMENT_KEYWORDS:
        value = getattr(doc.properties, name, None)
        if not value or keyword in present:
            continue
        if name == "filetags":
            rendered = ":" + ":".join(str(tag) for tag in value) + ":"
        else:
            rendered = _oneline(plain_text(value))
        if rendered:
            lines.append(f"#+{keyword.lower()}: {rendered}")
    if not lines:
        return ""
    return "\n".join(lines) + "\n\n"


def _all_nodes(contents: Any):
    """Every node under CONTENTS, for finding the keywords among them."""
    for child in contents:
        if isinstance(child, OrgNodeBase):
            yield child
            yield from _all_nodes(child.contents)


def _trailing(child: Any) -> str:
    """The whitespace org-element took out from after an inline node.

    Org consumes the space that ends an entity -- ``\\alpha `` is the
    entity and its terminator -- and records it as ``post-blank`` rather
    than leaving it in the text.  A renderer that ignores that writes
    ``\\alphaand``, which reads back as an entity nobody wrote.
    """
    if not isinstance(child, OrgNodeBase):
        return ""
    count = child.properties.get("post_blank")
    return " " * count if isinstance(count, int) and count > 0 else ""


def _is_block(child: Any) -> bool:
    return isinstance(child, OrgNodeBase) and child.type in BLOCKS


def _join(items: list[tuple[Any, str]], tight: bool = False) -> str:
    """Join rendered blocks, deciding where a blank line belongs.

    Two ordinary blocks are separated by one; two run-on line blocks --
    a stack of ``#+keyword:`` lines, a drawer's properties -- are not,
    because a blank line between them would end the run.
    """
    out: list[str] = []
    previous: str | None = None
    for child, text in items:
        if not text.strip():
            continue
        kind = child.type if isinstance(child, OrgNodeBase) else None
        packed = tight or (kind in LINE_BLOCKS and previous in LINE_BLOCKS)
        if out and not packed:
            out.append("\n")
        out.append(_newline(text))
        previous = kind
    return "".join(out)


def _newline(text: str) -> str:
    """Exactly one trailing newline, which is what every block promises."""
    return text.rstrip("\n") + "\n"


def _oneline(text: str) -> str:
    """Collapse to a single line, for somewhere a newline cannot go."""
    return " ".join(str(text).split())


def _stamp(stamp: Any) -> str:
    """An org timestamp, written the way org writes one."""
    raw = getattr(stamp, "raw_value", None)
    if raw:
        return str(raw)
    start = str(getattr(stamp, "start", "") or "")
    date, _, time = start.partition("T")
    body = f"{date} {time[:5]}".strip() if time else date
    active = getattr(stamp, "type", "active") != "inactive"
    return f"<{body}>" if active else f"[{body}]"
