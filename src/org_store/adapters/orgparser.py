# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reading org text into the canonical model, without Emacs.

``org-parser`` (tree-sitter based) gives an object tree of its own; this
turns that into the same :class:`~org_store.model.OrgDocument` shape that
``ox-json`` produces from Emacs, so everything above this layer neither
knows nor cares which engine read the file.

Two constructs org-element understands have no equivalent here, and both
are repaired at this boundary rather than left for callers to notice:

LaTeX
    There is no fragment or environment type at all.  ``$E = mc^2$``
    survives inside a paragraph as inert text, which is recoverable --
    but ``\\begin{equation}`` is actively mangled, its first word becoming
    an ``InlineEntity`` and the rest plain text.  Both are detected in the
    paragraph's *raw source*, which is intact either way, and promoted to
    real ``latex-fragment``/``latex-environment`` nodes.

Footnote definitions
    There is no block-level type.  ``[fn:1] the text`` parses as an
    ordinary paragraph that happens to begin with a footnote reference,
    which is exactly the signal used to promote it.

Both repairs happen here and nowhere else, so documents that came from
Emacs are untouched by them -- they already had the distinction.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..model import (
    DocumentProperties,
    OrgDocument,
    OrgNodeBase,
    Timestamp,
)

# Inline maths, in the four spellings org recognises.  Ordered longest
# construct first so ``\[..\]`` is not mistaken for an escaped bracket.
_LATEX_FRAGMENT = re.compile(
    r"""
      \\begin\{(?P<env>[^}]+)\}.*?\\end\{(?P=env)\}   # \begin{x}..\end{x}
    | \\\[.*?\\\]                                     # \[ .. \]
    | \\\(.*?\\\)                                     # \( .. \)
    | (?<!\$)\$[^$\n]+?\$(?!\$)                       # $ .. $
    """,
    re.VERBOSE | re.DOTALL,
)

_LATEX_ENVIRONMENT = re.compile(r"^\s*\\begin\{[^}]+\}.*\\end\{[^}]+\}\s*$", re.DOTALL)

#: ``[fn:label] text`` at the very start of a paragraph.
_FOOTNOTE_DEFINITION = re.compile(r"^\[fn:([^\]]+)\]\s*")


def parse(text: str) -> OrgDocument:
    """Read org TEXT into the canonical model."""
    import org_parser

    return _document(org_parser.loads(text))


def parse_file(path: Path) -> OrgDocument:
    """Read an org file into the canonical model."""
    return parse(Path(path).read_text(encoding="utf-8"))


# -- document ---------------------------------------------------------------


def _document(parsed: Any) -> OrgDocument:
    keywords = {kw.key.upper(): kw.value for kw in parsed.keywords}
    properties = DocumentProperties(
        title=_rich(keywords.get("TITLE")),
        author=_rich(keywords.get("AUTHOR")),
        date=_rich(keywords.get("DATE")),
        description=_rich(keywords.get("DESCRIPTION")),
        # ox-json hands back a split list; org-parser gives the raw
        # ``:a:b:`` string, so the normalising happens here.
        filetags=_split_tags(keywords.get("FILETAGS")),
    )
    # creator, email and language are facts about the Emacs that exported a
    # document, not about the file, so nothing here can supply them.

    contents: list[Any] = []
    preamble = _section(parsed.body, keywords=parsed.keywords)
    if preamble is not None:
        contents.append(preamble)
    contents.extend(_headline(child) for child in parsed.children)
    return OrgDocument(properties=properties, contents=contents)


def _headline(heading: Any) -> OrgNodeBase:
    """One heading and everything under it."""
    properties: dict[str, Any] = {
        "level": heading.level,
        "raw-value": heading.title_text.strip(),
        # Both the title and a table cell arrive padded out to the
        # column org aligns them in.  That padding is presentation, and
        # ox-json drops it too, so the two engines agree on the content.
        "title": _rich(str(heading.title).strip()),
        "tags": list(heading.heading_tags),
    }
    if heading.todo:
        properties["todo-keyword"] = str(heading.todo)
    if heading.priority:
        properties["priority"] = str(heading.priority)
    for name in ("scheduled", "deadline", "closed"):
        stamp = _timestamp(getattr(heading, name, None))
        if stamp is not None:
            properties[name] = stamp

    contents: list[Any] = []
    # org-element gives a heading a section whenever anything sits below
    # its title line -- a planning line and a property drawer both count,
    # even though this model keeps them in the headline's properties.  An
    # empty section here is what keeps the two engines' structure, and so
    # their refs, identical.
    below = bool(heading.properties) or any(
        properties.get(name) is not None for name in ("scheduled", "deadline", "closed")
    )
    section = _section(heading.body) or (_node("section", {}) if below else None)
    if section is not None:
        contents.append(section)
    contents.extend(_headline(child) for child in heading.children)

    return _node(
        "headline",
        properties,
        contents,
        # org-parser resolves tag inheritance itself, so these mean the
        # same as ox-json's, and the index gets the same answer either way.
        tags_all=list(heading.tags),
        drawer={key: str(value) for key, value in (heading.properties or {}).items()},
    )


def _section(body: Any, keywords: Any = None) -> OrgNodeBase | None:
    """The content directly under a heading, or before the first one."""
    contents: list[Any] = []
    for keyword in keywords or ():
        contents.append(
            _node("keyword", {"key": keyword.key.upper(), "value": str(keyword.value)})
        )
    contents.extend(_elements(body))
    if not contents:
        return None
    return _node("section", {}, contents)


# -- elements ---------------------------------------------------------------


def _element(element: Any) -> Any:
    """One block-level element, or None for something not worth keeping."""
    handler = _ELEMENTS.get(type(element).__name__)
    return handler(element) if handler else _unknown(element)


def _paragraph(element: Any) -> Any:
    """A paragraph, after the LaTeX and footnote repairs.

    The raw source is used for detection rather than the parsed parts,
    because the parts are exactly what the parser gets wrong here.
    """
    raw = element.body_text or ""

    if _LATEX_ENVIRONMENT.match(raw):
        # The trailing newline is part of the element for org-element, so
        # stripping it here would put the two engines out of step.
        return _latex("latex-environment", raw.lstrip(), "block")

    footnote = _FOOTNOTE_DEFINITION.match(raw)
    if footnote:
        rest = raw[footnote.end() :]
        return _node(
            "footnote-definition",
            {"label": footnote.group(1)},
            [_node("paragraph", {}, _inline_from_source(rest))],
        )

    return _node("paragraph", {}, _inline_from_source(raw))


def _inline_from_source(raw: str) -> list[Any]:
    """Parse RAW into inline nodes, promoting any maths found in it.

    The text either side of a fragment is re-parsed on its own, so the
    parser never sees the maths and cannot mangle it.
    """
    out: list[Any] = []
    position = 0
    for match in _LATEX_FRAGMENT.finditer(raw):
        out.extend(_inline_parts(raw[position : match.start()]))
        out.append(_latex("latex-fragment", match.group(0), "inline"))
        position = match.end()
    out.extend(_inline_parts(raw[position:]))
    return _merge_text(out)


def _inline_parts(raw: str) -> list[Any]:
    """Parse a run of text into inline nodes.

    ``from_source`` expects something that stands alone as a paragraph and
    raises on fragments that do not -- a run left over from cutting maths
    out of the middle of a sentence often will not.  Losing the markup in
    such a run is a much smaller loss than losing the run, so it falls
    back to plain text.
    """
    if not raw:
        return []
    from org_parser.text import RichText

    try:
        parts = RichText.from_source(raw).parts
    except (ValueError, IndexError):
        return [raw]
    return [_inline(part) for part in parts]


def _latex(kind: str, value: str, display: str) -> OrgNodeBase:
    return _node(kind, {"value": value, "display": display, "dialect": "latex"})


def _block(kind: str, extra: dict[str, Any] | None = None):
    """A block whose whole content is one verbatim string.

    ``body`` rather than ``body_text``: for these elements the first holds
    the text between the delimiters and the second is empty.
    """

    def handler(element: Any) -> OrgNodeBase:
        properties: dict[str, Any] = {"value": str(element.body or "")}
        for key, attribute in (extra or {}).items():
            value = getattr(element, attribute, None)
            properties[key] = str(value) if value is not None else None
        return _node(kind, properties)

    return handler


def _container(kind: str):
    """A block that holds other elements rather than verbatim text."""

    def handler(element: Any) -> OrgNodeBase:
        return _node(kind, {}, _elements(element.body))

    return handler


def _elements(body: Any) -> list[Any]:
    """Walk a list of block elements, recording the gaps between them.

    Blank lines are not content and do not become nodes, but they are not
    nothing either: org-element records the whitespace that followed an
    element as its ``post-blank``, and everything that flattens a document
    back to text relies on it to keep the last word of one block off the
    first word of the next.  org-parser reports the blank lines as
    elements of their own, so they are counted here and attached to the
    element they follow.
    """
    out: list[Any] = []
    blanks = 0
    for element in body or ():
        if type(element).__name__ == "BlankLine":
            blanks += 1
            continue
        if blanks and out:
            _set_post_blank(out[-1], blanks)
        blanks = 0
        node = _element(element)
        if node is not None:
            out.extend(node if isinstance(node, list) else [node])
    if blanks and out:
        _set_post_blank(out[-1], blanks)
    return out


def _set_post_blank(node: Any, count: int) -> None:
    if isinstance(node, OrgNodeBase):
        node.properties.post_blank = count


def _list(element: Any) -> OrgNodeBase:
    items = list(element.items)
    ordered = any(str(getattr(i, "bullet", "")).strip().endswith((".", ")")) for i in items)
    return _node(
        "plain-list",
        {"type": "ordered" if ordered else "unordered"},
        [_item(item) for item in items],
    )


def _item(item: Any) -> OrgNodeBase:
    properties: dict[str, Any] = {"bullet": str(item.bullet or "- ")}
    checkbox = getattr(item, "checkbox", None)
    if checkbox is not None:
        properties["checkbox"] = _CHECKBOX.get(str(checkbox), "off")
    tag = getattr(item, "item_tag", None)
    if tag:
        properties["tag"] = _rich(tag)

    # An item's own text is its first line; anything blockier beneath it
    # arrives separately in body.
    contents: list[Any] = []
    first = getattr(item, "first_line", None)
    if first is not None and str(first).strip():
        contents.append(_node("paragraph", {}, _rich(first)))
    contents.extend(_elements(item.body))
    return _node("item", properties, contents)


#: org-parser reports the character between the brackets, not the whole
#: marker, and an unticked box comes back as a space.
_CHECKBOX = {"X": "on", "x": "on", " ": "off", "-": "trans"}


def _table(element: Any) -> OrgNodeBase:
    rows = []
    for row in element.rows:
        if type(row).__name__ == "TableRuleRow":
            rows.append(_node("table-row", {"type": "rule"}))
            continue
        rows.append(
            _node(
                "table-row",
                {"type": "standard"},
                [
                    _node(
                        "table-cell", {}, _inline_from_source(str(cell.value).strip())
                    )
                    for cell in row.cells
                ],
            )
        )
    return _node("table", {"type": "org"}, rows)


def _keyword(element: Any) -> OrgNodeBase:
    return _node(
        "keyword",
        {"key": str(element.key).upper(), "value": str(element.value)},
    )


def _unknown(element: Any) -> Any:
    """Anything with no mapping yet, kept rather than dropped.

    The canonical model tolerates unrecognised types, so a construct this
    walker has not learned survives as its own source text instead of
    silently disappearing from the document.
    """
    raw = getattr(element, "body_text", None) or getattr(element, "text", None)
    if raw is None or not str(raw).strip():
        return None
    return _node(
        type(element).__name__.lower(), {"value": str(raw)}
    )


#: Which handler builds each org-parser element.
_ELEMENTS: dict[str, Any] = {
    # BlankLine never reaches here: _elements consumes them into the
    # post-blank of the element they follow.
    "Comment": lambda element: None,
    "Paragraph": _paragraph,
    "SourceBlock": _block("src-block", {"language": "language", "parameters": "switches"}),
    "ExampleBlock": _block("example-block"),
    "FixedWidthBlock": _block("fixed-width"),
    "CommentBlock": _block("comment-block"),
    "ExportBlock": _block("export-block"),
    "QuoteBlock": _container("quote-block"),
    "CenterBlock": _container("center-block"),
    "VerseBlock": _container("verse-block"),
    "SpecialBlock": _container("special-block"),
    "List": _list,
    "Table": _table,
    "Keyword": _keyword,
    "HorizontalRule": lambda element: _node("horizontal-rule", {}),
}


# -- inline objects ---------------------------------------------------------


def _inline(part: Any) -> Any:
    """One inline object: a node, or a bare string for plain text."""
    name = type(part).__name__
    if name == "PlainText":
        return str(part)
    handler = _INLINE.get(name)
    if handler:
        return handler(part)
    # Unrecognised markup keeps its source rather than vanishing.
    return str(part)


def _emphasis(kind: str):
    def handler(part: Any) -> OrgNodeBase:
        return _node(kind, {}, _merge_text([_inline(p) for p in _parts(part.body)]))

    return handler


def _verbatim(kind: str):
    def handler(part: Any) -> OrgNodeBase:
        return _node(kind, {"value": str(part.body)})

    return handler


def _link(part: Any) -> OrgNodeBase:
    raw = str(part.path)
    scheme, separator, rest = raw.partition(":")
    properties = {
        "raw-link": raw,
        "type": scheme if separator else "fuzzy",
        "path": rest if separator else raw,
        "format": "bracket",
    }
    description = getattr(part, "description", None)
    contents = (
        _merge_text([_inline(p) for p in _parts(description)]) if description else []
    )
    return _node("link", properties, contents)


def _footnote_reference(part: Any) -> OrgNodeBase:
    return _node("footnote-reference", {"label": str(part.label), "type": "standard"})


def _timestamp_node(part: Any) -> OrgNodeBase:
    """An inline timestamp.

    Shaped the way ``ox-json`` shapes one: the resolved bounds sit on the
    node, and the kind and source text in its properties.  A planning
    timestamp is a different thing again -- see :func:`_timestamp`.
    """
    start = _stamp_iso(part, "start")
    end = _stamp_iso(part, "end")
    kind = "active" if getattr(part, "is_active", False) else "inactive"
    properties: dict[str, Any] = {"raw-value": str(part)}
    if end is not None and end != start:
        kind += "-range"
        # Same day, different clock times, is a time range; otherwise the
        # range spans dates.
        properties["range-type"] = (
            "timerange" if start[:10] == end[:10] else "daterange"
        )
    properties["type"] = kind
    return _node("timestamp", properties, start=start, end=end or start)


def _entity(part: Any) -> OrgNodeBase:
    return _node(
        "entity",
        {"name": str(part.name), "use-brackets-p": bool(getattr(part, "has_braces", False))},
    )


_INLINE: dict[str, Any] = {
    "Bold": _emphasis("bold"),
    "Italic": _emphasis("italic"),
    "Underline": _emphasis("underline"),
    "StrikeThrough": _emphasis("strike-through"),
    "Subscript": _emphasis("subscript"),
    "Superscript": _emphasis("superscript"),
    "Code": _verbatim("code"),
    "Verbatim": _verbatim("verbatim"),
    "RegularLink": _link,
    "PlainLink": lambda part: _node(
        "link", {"raw-link": str(part.path), "format": "plain"}
    ),
    "AngleLink": lambda part: _node(
        "link", {"raw-link": str(part.path), "format": "angle"}
    ),
    "FootnoteReference": _footnote_reference,
    "InlineEntity": _entity,
    "CompletionCounter": lambda part: _node(
        "statistics-cookie", {"value": str(part)}
    ),
    "LineBreak": lambda part: _node("line-break", {}),
    "Timestamp": lambda part: _timestamp_node(part),
    "Target": lambda part: _node("target", {"value": str(part)}),
    "RadioTarget": lambda part: _node("radio-target", {"value": str(part)}),
}


# -- helpers ----------------------------------------------------------------


def _node(
    kind: str, properties: dict[str, Any], contents: list[Any] | None = None, **extra: Any
) -> OrgNodeBase:
    """Build a canonical node, routed to its class by type.

    Going through the document model rather than instantiating a class
    directly means the discriminated union picks the type, so this walker
    never has to know which node types have a dedicated class.
    """
    payload: dict[str, Any] = {
        "$$data_type": "org-node",
        "type": kind,
        "properties": {k: v for k, v in properties.items() if v is not None},
        "contents": contents or [],
    }
    payload.update({k.replace("_", "-"): v for k, v in extra.items()})
    (built,) = OrgDocument.model_validate({"contents": [payload]}).contents
    return built


def _parts(value: Any) -> list[Any]:
    """The inline objects inside VALUE.

    Emphasis holds a plain list of parts while a paragraph holds a
    ``RichText`` wrapping one, so both spellings are accepted rather than
    every caller having to know which it has.
    """
    if value is None:
        return []
    parts = getattr(value, "parts", None)
    if parts is not None:
        return list(parts)
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _rich(value: Any) -> list[Any]:
    """A secondary string -- a title or keyword value -- as model contents."""
    if value is None:
        return []
    if isinstance(value, str) and not hasattr(value, "parts"):
        return [str(value)]
    return _merge_text([_inline(part) for part in _parts(value)])


def _merge_text(items: list[Any]) -> list[Any]:
    """Join neighbouring strings, and drop the empty ones.

    The parser emits a separate part per line and a trailing empty one;
    left alone those become several adjacent strings in ``contents``,
    which is noise rather than structure.
    """
    out: list[Any] = []
    for item in items:
        if isinstance(item, str):
            if not item:
                continue
            if out and isinstance(out[-1], str):
                out[-1] += item
                continue
        out.append(item)
    return out


def _split_tags(value: Any) -> list[str]:
    """``:physics:dune:`` -> ``["physics", "dune"]``."""
    if value is None:
        return []
    return [tag for tag in str(value).split(":") if tag]


def _timestamp(stamp: Any) -> Timestamp | None:
    """One of org-parser's timestamps as a canonical value object."""
    if stamp is None:
        return None
    start = _stamp_iso(stamp, "start")
    active = bool(getattr(stamp, "is_active", False))
    # The day name is not derivable from the date alone without assuming a
    # calendar, and org writes it, so it is kept in the raw form.
    date, _, clock = (start or "").partition("T")
    body = f"{date}{_dayname(stamp, 'start')}"
    if clock:
        body += f" {clock[:5]}"
    return Timestamp(
        start=start,
        end=_stamp_iso(stamp, "end") or start,
        type="active" if active else "inactive",
        raw_value=f"<{body}>" if active else f"[{body}]",
    )


def _dayname(stamp: Any, side: str) -> str:
    name = getattr(stamp, f"{side}_dayname", None)
    return f" {name}" if name else ""


def _stamp_iso(stamp: Any, side: str) -> str | None:
    """``2026-08-24`` or ``2026-08-24T14:03:00`` for one end of a timestamp."""
    year = getattr(stamp, f"{side}_year", None)
    if year is None:
        return None
    month = getattr(stamp, f"{side}_month", 1) or 1
    day = getattr(stamp, f"{side}_day", 1) or 1
    date = f"{year:04d}-{month:02d}-{day:02d}"
    hour = getattr(stamp, f"{side}_hour", None)
    if hour is None:
        return date
    minute = getattr(stamp, f"{side}_minute", 0) or 0
    return f"{date}T{hour:02d}:{minute:02d}:00"
