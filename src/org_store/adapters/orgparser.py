# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reading org text into the canonical model, without Emacs.

``org-parser`` (tree-sitter based) gives an object tree of its own; this
turns that into the same :class:`~org_store.model.OrgDocument` shape that
``ox-json`` produces from Emacs, so everything above this layer neither
knows nor cares which engine read the file.

Several things org-element gets right have no equivalent here, and each
is repaired at this boundary rather than left for callers to notice.
Every repair happens here and nowhere else, so documents that came from
Emacs are untouched by them -- they were already right.

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

Block content
    A line inside a block that looks like a heading ends the block where
    it stands, losing the rest of it.  Such lines are masked before the
    parser runs -- see :func:`_mask_blocks`.

Ordered lists
    An item's bullet comes back as the punctuation alone, so the counter
    is put back beside it -- see :func:`_bullet`.

Bare links
    The scheme is kept apart from the path, and a link built from the
    path alone is a different address -- see :func:`_bare_link`.

Keywords
    A document's keywords arrive in a list of their own rather than among
    its elements, and its property drawer as a mapping.  Both are merged
    back into the order they were written in, so a ``#+name:`` still
    names the block after it -- see :func:`_in_source_order`.

Indentation
    A list item's continuation lines keep the indent that put them under
    the bullet.  It is structure rather than text, and leaving it in
    means it is added to on every write -- see :func:`_dedent`.
"""

from __future__ import annotations

import gc
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..model import (
    DocumentProperties,
    OrgDocument,
    OrgNodeBase,
    Timestamp,
    TimestampRepeat,
)
from ..store import StoreError

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

#: Where a block starts and stops.  A block ends only at its own
#: ``#+end_``: an ``#+end_example`` inside a source block is content.
_BLOCK_BEGIN = re.compile(r"^[ \t]*#\+begin_(\S+)", re.IGNORECASE)
_BLOCK_END = re.compile(r"^[ \t]*#\+end_(\S+)", re.IGNORECASE)

#: A line inside a block that the parser would read as org rather than as
#: content: one that would start a heading, or a keyword line.
_LOOKS_LIKE_ORG = re.compile(r"^([ \t]*)([*#])")

#: What stands in for that first character while the parser runs.  Private
#: use codepoints, one per character masked so that unmasking knows which
#: it was.  Text already holding one is left alone rather than mangled.
_MASKS = {"*": "\ue000", "#": "\ue001"}
_UNMASKS = {mask: character for character, mask in _MASKS.items()}
_MASKED = re.compile("[\ue000\ue001]")

#: Node types that are a block, and so have unmasked their own content.
_BLOCK_TYPES = frozenset(
    {
        "src-block",
        "example-block",
        "export-block",
        "comment-block",
        "quote-block",
        "center-block",
        "verse-block",
        "special-block",
    }
)

#: ``,*`` and ``,#+`` are how org writes block content that would
#: otherwise be read as org.  The comma says "this is content" rather
#: than being part of it, so it comes off here and goes back on in the
#: renderer -- which is what org-element does with the same text.
_ESCAPED_LINE = re.compile(r"^([ \t]*),(,*(?:\*|#\+))", re.MULTILINE)


def parse(text: str) -> OrgDocument:
    """Read org TEXT into the canonical model."""
    import org_parser

    with _tree_held():
        return _document(org_parser.loads(_mask_blocks(text)), _drawer_line(text))


@contextmanager
def _tree_held():
    """Keep the parse tree alive while its nodes are being read.

    org-parser's elements each hold a ``tree_sitter.Node``, which is only
    valid while the tree it came from is.  On a document of any size the
    collector can take that tree while elements still refer to it, and
    reading one then reads freed memory: the process dies with a
    segmentation fault, which no ``except`` can catch and no caller can
    report.  It is reproducible, and it goes away entirely with the
    cyclic collector switched off.

    So it is switched off for the duration of a parse.  Reference
    counting still frees everything that is not part of a cycle, so this
    costs a little memory rather than leaking; the alternative is a
    parser that occasionally kills the program.
    """
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


def _mask_blocks(text: str) -> str:
    """Hide block content that the parser would mistake for org syntax.

    A ``*`` at the start of a line inside a block is content, and org
    reads it as such.  This parser does not: it ends the block where that
    line stands, reports an unterminated block, and reads the rest as a
    heading -- so the block's content is silently lost.

    Org's own escape, a leading comma, is no help, because the same
    happens to the many lines nobody escaped.  So the offending character
    is masked before the parser sees it and put back once the block's
    text is in hand.  Nothing the document says is changed; the mask does
    not outlive the parse.
    """
    if _MASKED.search(text):
        return text
    out: list[str] = []
    ending: str | None = None
    for line in text.splitlines(keepends=True):
        begin = _BLOCK_BEGIN.match(line) if ending is None else None
        if begin is not None:
            ending = begin.group(1).lower()
        elif ending is not None:
            end = _BLOCK_END.match(line)
            if end is not None and end.group(1).lower() == ending:
                ending = None
            else:
                line = _LOOKS_LIKE_ORG.sub(
                    lambda match: match.group(1) + _MASKS[match.group(2)], line, count=1
                )
        out.append(line)
    return "".join(out)


def _unmask(text: str) -> str:
    """Put back what :func:`_mask_blocks` hid, for one string."""
    if not _MASKED.search(text):
        return text
    return _MASKED.sub(lambda match: _UNMASKS[match.group(0)], text)


def _unmask_tree(nodes: list[Any]) -> None:
    """Put it back throughout a block's children, in place.

    A block nested inside this one has already unmasked its own text, so
    the walk stops at one.
    """
    for index, node in enumerate(nodes):
        if isinstance(node, str):
            nodes[index] = _unmask(node)
            continue
        if not isinstance(node, OrgNodeBase) or node.type in _BLOCK_TYPES:
            continue
        value = node.properties.get("value")
        if isinstance(value, str):
            node.properties.value = _unmask(value)
        _unmask_tree(node.contents)


def _unescape_lines(text: str) -> str:
    """Take org's comma escape off a verbatim block's content."""
    return _ESCAPED_LINE.sub(r"\1\2", text)


def _drawer_line(text: str) -> int | None:
    """Which line the file-level property drawer starts on, if any.

    org-parser hands back the drawer's contents but not where they were,
    and a document keeps its parts in the order they were written, so the
    line is worked out here instead.
    """
    for number, line in enumerate(text.splitlines()):
        if line.startswith("*"):
            return None
        if line.strip().upper() == ":PROPERTIES:":
            return number
    return None


def parse_file(path: Path, *, isolated: bool = True) -> OrgDocument:
    """Read an org file into the canonical model.

    By default the parse happens in a child process.  org-parser can die
    on a document rather than raise on it -- a segmentation fault out of
    tree-sitter, which no ``except`` can catch -- and a file store reads
    whatever files it is pointed at.  In this process that takes the
    whole program down with no message; in a child it is one document
    reported as unreadable, which is what every other bad document does.

    It costs a fork and a JSON round trip, about half again as long as
    parsing in this process.  Pass ``isolated=False`` where the text is
    already known to be safe, as the tests do.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError(f"{path}: {exc}") from exc
    return _parse_isolated(text, str(path)) if isolated else parse(text)


def _parse_isolated(text: str, name: str, target: Any = None) -> OrgDocument:
    """Parse TEXT in a child process, reporting a death as a failed read."""
    import multiprocessing

    try:
        context = multiprocessing.get_context("fork")
    except ValueError:  # pragma: no cover - fork is absent only on Windows
        return parse(text)

    import org_parser  # noqa: F401 - imported here so the child need not

    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(
        target=target or _parse_into, args=(text, sender), daemon=True
    )
    child.start()
    sender.close()
    try:
        answer = receiver.recv() if receiver.poll(_PARSE_TIMEOUT) else None
    except EOFError:
        answer = None
    finally:
        receiver.close()
        child.join(timeout=5)
        if child.is_alive():  # pragma: no cover - only a hung child gets here
            child.terminate()

    if answer is None:
        raise StoreError(
            f"{name}: the org parser died reading this file "
            f"(exit {child.exitcode}); the file is not readable here"
        )
    kind, payload = answer
    if kind == "error":
        raise StoreError(f"{name}: {payload}")
    return OrgDocument.model_validate_json(payload)


#: How long a child gets before it is taken to be stuck.  Generous: this
#: is a backstop against a hang, not a performance budget.
_PARSE_TIMEOUT = 120


def _parse_into(text: str, sender: Any) -> None:
    """Parse TEXT and send it back as canonical JSON.  Runs in the child."""
    try:
        sender.send(("ok", parse(text).model_dump_json(by_alias=True)))
    except Exception as exc:  # noqa: BLE001 - the parent re-raises it
        sender.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        sender.close()


# -- document ---------------------------------------------------------------


def _document(parsed: Any, drawer_line: int | None = None) -> OrgDocument:
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
    preamble = _section(
        parsed.body,
        keywords=parsed.keywords,
        extra=_file_drawer(parsed.properties, drawer_line),
    )
    if preamble is not None:
        contents.append(preamble)
    contents.extend(_headline(child) for child in parsed.children)
    return OrgDocument(properties=properties, contents=contents)


@dataclass(frozen=True)
class _FileProperties:
    """A file-level property drawer, and the line it was written on.

    org-parser reports the drawer as a mapping hanging off the document
    rather than as an element in its body, so there is nothing for
    :func:`_in_source_order` to place.  This stands in for an element, and
    is dispatched like any other.
    """

    line: int
    items: tuple[tuple[str, str], ...]


def _file_drawer(properties: Any, line: int | None) -> list[_FileProperties]:
    """The document's own property drawer, if it has one."""
    items = tuple((str(key), str(value)) for key, value in (properties or {}).items())
    if not items:
        return []
    return [_FileProperties(line=line if line is not None else 0, items=items)]


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


def _section(body: Any, keywords: Any = None, extra: Any = None) -> OrgNodeBase | None:
    """The content directly under a heading, or before the first one."""
    contents = _elements(_in_source_order(body, keywords, extra))
    if not contents:
        return None
    return _node("section", {}, contents)


def _in_source_order(body: Any, keywords: Any = None, extra: Any = None) -> list[Any]:
    """Put a document's keywords and drawer back among its elements.

    org-parser hands back a document's keywords in a list of their own and
    its property drawer as a mapping, neither of them among the elements
    they were written between.  Emitting the keywords first instead --
    which is what the file usually looks like anyway -- moves a
    ``#+name:`` away from the block it names, and that is the one place a
    keyword's position carries meaning.

    Each of those *does* say which line it was on.  The elements do not:
    reading ``line`` from one is what crashes tree-sitter on a document
    of any size, so where each element sits is counted from how many
    lines its own source takes up instead.
    """
    pending = sorted(
        [*(keywords or ()), *(extra or ())], key=lambda item: item.line
    )
    out: list[Any] = []
    position = 0
    for element in body or ():
        while pending and pending[0].line <= position:
            out.append(pending.pop(0))
            position += _span(out[-1])
        out.append(element)
        position += _span(element)
    out.extend(pending)
    return out


def _span(element: Any) -> int:
    """How many lines of the source an element takes up."""
    if isinstance(element, _FileProperties):
        return len(element.items) + 2  # :PROPERTIES:, one per key, :END:
    text = str(element)
    return text.count("\n") or 1


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


#: Whitespace at the start of a continuation line.  Inside a list item
#: that is the indent putting the line under the bullet, not something
#: the paragraph says.
_CONTINUATION_INDENT = re.compile(r"\n[ \t]+")


def _dedent(node: Any) -> None:
    """Take the indent off a paragraph's continuation lines, in place.

    A list item's second line onwards arrives with the indent that put it
    under the bullet still in the text.  The renderer indents an item's
    content itself, so leaving it there means the two are added together:
    a three-line item gains two spaces on one write and loses them on the
    next, and no version of the file is stable.
    """
    if not isinstance(node, OrgNodeBase):
        return
    for index, child in enumerate(node.contents):
        if isinstance(child, str):
            node.contents[index] = _CONTINUATION_INDENT.sub("\n", child)
        else:
            _dedent(child)


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
        content = _unmask(str(element.body or ""))
        if kind in _ESCAPING_BLOCKS:
            content = _unescape_lines(content)
        properties: dict[str, Any] = {"value": content}
        for key, attribute in (extra or {}).items():
            value = getattr(element, attribute, None)
            properties[key] = str(value) if value is not None else None
        return _node(kind, properties)

    return handler


#: Blocks whose content org writes with its comma escape.  A quote block
#: is not one: what it holds is elements, and org neither escapes nor
#: unescapes there, so neither does this.
_ESCAPING_BLOCKS = frozenset(
    {"src-block", "example-block", "export-block", "comment-block"}
)


def _container(kind: str, extra: dict[str, Any] | None = None):
    """A block that holds other elements rather than verbatim text."""

    def handler(element: Any) -> OrgNodeBase:
        contents = _elements(element.body)
        _unmask_tree(contents)
        properties: dict[str, Any] = {}
        for key, attribute in (extra or {}).items():
            value = getattr(element, attribute, None)
            properties[key] = str(value) if value is not None else None
        return _node(kind, properties, contents)

    return handler


def _indent(element: Any) -> list[Any]:
    """Indented content: the elements inside it, and how far in they sat.

    org-element has no such element -- indentation is not part of what a
    document says -- so this yields what is *inside* rather than a node
    standing for the indent.  The width is recorded on each block so that
    a file written back keeps the shape its author gave it; nothing else
    reads it, and a document from Emacs simply has none.
    """
    width = len(str(getattr(element, "indent", "") or ""))
    out: list[Any] = []
    for node in _elements(element.body):
        if isinstance(node, OrgNodeBase):
            if node.type == "paragraph":
                _dedent(node)
            if width:
                node.properties.indent = width
        out.append(node)
    return out


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
    properties: dict[str, Any] = {"bullet": _bullet(item)}
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
    for child in _elements(item.body):
        if isinstance(child, OrgNodeBase) and child.type == "paragraph":
            _dedent(child)
        contents.append(child)
    return _node("item", properties, contents)


def _bullet(item: Any) -> str:
    """The bullet an item is written with, counter and all.

    org-parser reports an ordered item's bullet as the punctuation alone,
    ``.`` or ``)``, and keeps the number beside it.  Storing only the
    punctuation loses the numbering: ``1. one`` comes back as ``. one``.
    """
    bullet = str(item.bullet or "- ")
    counter = getattr(item, "ordered_counter", None)
    if counter and bullet in (".", ")"):
        return f"{counter}{bullet}"
    return bullet


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


def _affiliated(key: str):
    """A keyword that belongs to the element after it.

    org-parser gives ``#+caption:`` and its kin a class each, with the
    key implied by the class rather than carried on it.  They are
    keywords all the same, and become ``keyword`` nodes like any other;
    what makes them affiliated is that the renderer keeps them against
    the block they name -- see :data:`org_store.render.renderer.AFFILIATED`.
    """

    def handler(element: Any) -> OrgNodeBase:
        return _node("keyword", {"key": key, "value": str(element.value)})

    return handler


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
    # A special block is named by what follows ``#+begin_``, which is the
    # only thing distinguishing one from another.
    "SpecialBlock": _container(
        "special-block", {"type": "name", "parameters": "parameters"}
    ),
    "Indent": _indent,
    "List": _list,
    "Table": _table,
    "Keyword": _keyword,
    "AffiliatedKeyword": _keyword,
    "CaptionKeyword": _affiliated("CAPTION"),
    "TblnameKeyword": _affiliated("TBLNAME"),
    "ResultsKeyword": _affiliated("RESULTS"),
    "PlotKeyword": _affiliated("PLOT"),
    "HorizontalRule": lambda element: _node("horizontal-rule", {}),
    # A file-level property drawer, which org-parser reports apart from
    # the elements; see :class:`_FileProperties`.
    "_FileProperties": lambda element: _node(
        "property-drawer",
        {},
        [
            _node("node-property", {"key": key, "value": value})
            for key, value in element.items
        ],
    ),
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


def _bare_link(part: Any, form: str) -> OrgNodeBase:
    """A link written without brackets, plain or in angles.

    org-parser splits one the way org-element does, keeping the scheme in
    ``link_type`` and the rest in ``path``, so a link built from the path
    alone loses the scheme: ``https://example.org`` becomes
    ``//example.org``, which is a different address.
    """
    scheme = str(getattr(part, "link_type", "") or "")
    path = str(part.path)
    return _node(
        "link",
        {
            "raw-link": f"{scheme}:{path}" if scheme else path,
            "type": scheme or "fuzzy",
            "path": path,
            "format": form,
        },
    )


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
    "PlainLink": lambda part: _bare_link(part, "plain"),
    "AngleLink": lambda part: _bare_link(part, "angle"),
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
    # A repeater and a warning delay are part of what the timestamp says,
    # and org writes them inside the brackets.
    for marker in (getattr(stamp, "repeater", None), getattr(stamp, "delay", None)):
        if marker is not None:
            body += f" {marker.mark}{marker.value}{marker.unit}"
    return Timestamp(
        start=start,
        end=_stamp_iso(stamp, "end") or start,
        type="active" if active else "inactive",
        raw_value=f"<{body}>" if active else f"[{body}]",
        repeater=_repeat(getattr(stamp, "repeater", None), _REPEATER_TYPES),
        warning=_repeat(getattr(stamp, "delay", None), _WARNING_TYPES),
    )


#: How org spells a repeater or delay, as org-element names it.
_REPEATER_TYPES = {"+": "cumulate", "++": "catch-up", ".+": "restart"}
_WARNING_TYPES = {"-": "all", "--": "first"}
_UNITS = {"h": "hour", "d": "day", "w": "week", "m": "month", "y": "year"}


def _repeat(marker: Any, types: dict[str, str]) -> TimestampRepeat | None:
    """One of org-parser's repeat markers, named the way ox-json names it."""
    if marker is None:
        return None
    return TimestampRepeat(
        type=types.get(str(marker.mark), str(marker.mark)),
        unit=_UNITS.get(str(marker.unit), str(marker.unit)),
        value=marker.value,
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
