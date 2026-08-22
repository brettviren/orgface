# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The canonical model for org content.

The shape is ``ox-json``'s: every org-element node becomes an object
carrying ``$$data_type``, ``type``, ``ref``, ``properties`` and
``contents``, where ``contents`` holds child nodes and bare strings of
text.  Modelling that shape -- rather than inventing one -- means the
Emacs ingestion path (:mod:`org_store` talking to ``ox-json``) needs no
translation at all, and the Emacs-free path only has to reproduce it.

Two rules keep this model honest as a canonical form:

Nothing is discarded.
    Every model here allows extra fields.  ``ox-json`` puts org-element
    internals in ``properties`` (``post-blank``, ``mode``, ``secondary``)
    and hangs a few derived values off the node itself (a headline's
    ``tags-all`` and ``drawer``, a link's ``target-ref``).  Only the ones
    worth naming are declared; the rest survive validation and round-trip
    untouched instead of being silently dropped.

Known types are typed.
    ``contents`` is a discriminated union over the node types we actually
    operate on, falling back to :class:`GenericNode` for the rest of
    org-element's vocabulary.  The typed set can grow without touching
    anything else -- add a class, add it to :data:`_TYPED_NODES`.

Positions are not part of this model.  A node's place in the source text
is recomputed from the live file whenever an edit needs it, never stored.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
)

#: Bumped whenever a change to this module alters the stored JSON shape.
SCHEMA_VERSION = 1


def _kebab(name: str) -> str:
    """``raw_value`` -> ``raw-value``, matching org-element's property names.

    ``ox-json`` emits org-element's property keywords with the leading
    colon stripped, so ``:raw-value`` arrives as ``raw-value``.
    """
    return name.replace("_", "-")


class _Model(BaseModel):
    """Base for everything here: kebab-case aliases, nothing discarded."""

    model_config = ConfigDict(
        alias_generator=_kebab,
        populate_by_name=True,
        extra="allow",
    )

    def get(self, name: str, default: Any = None) -> Any:
        """Read a property by its Python name, declared or not.

        The alias generator only applies to *declared* fields, so with
        ``extra="allow"`` an undeclared property keeps its wire spelling:
        a headline's ``level`` is reachable as an attribute but a generic
        node's ``todo-keyword`` is not.  Consumers should not each have to
        know which of the two they are holding, so both are tried here.
        """
        value = getattr(self, name, None)
        if value is not None:
            return value
        extra = self.__pydantic_extra__ or {}
        return extra.get(_kebab(name), default)


# -- value objects ----------------------------------------------------------


class TimestampRepeat(_Model):
    """A timestamp's repeater or warning period.

    All three fields are null when the timestamp carries neither, which is
    how ``ox-json`` represents "absent" here.
    """

    type: str | None = None
    unit: str | None = None
    value: int | None = None


class Timestamp(_Model):
    """An org timestamp, as ``ox-json`` renders one.

    This is a value object rather than a node: it appears *inside*
    ``properties`` (a headline's ``scheduled``/``deadline``/``closed``),
    tagged ``$$data_type: timestamp``, not in ``contents``.
    """

    data_type: Literal["timestamp"] = Field("timestamp", alias="$$data_type")
    start: str | None = None
    end: str | None = None
    type: str | None = None  # active | inactive
    raw_value: str | None = None
    repeater: TimestampRepeat | None = None
    warning: TimestampRepeat | None = None


# -- node properties --------------------------------------------------------


class NodeProperties(_Model):
    """Properties shared by every node type.

    ``ox-json`` attaches these to essentially everything; they are
    org-element's own bookkeeping rather than content, but dropping them
    would make the model lossy.
    """

    post_blank: int | None = None
    mode: str | None = None
    secondary: list[str] | None = None


class HeadlineProperties(NodeProperties):
    level: int | None = None
    true_level: int | None = None
    raw_value: str | None = None
    title: list[OrgContent] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    todo_keyword: str | None = None
    todo_type: str | None = None
    # org-element stores a character code; ox-json hands back "A".
    priority: str | int | None = None
    scheduled: Timestamp | None = None
    deadline: Timestamp | None = None
    closed: Timestamp | None = None
    archivedp: bool | None = None
    commentedp: bool | None = None
    footnote_section_p: bool | None = None


class SrcBlockProperties(NodeProperties):
    language: str | None = None
    value: str | None = None
    switches: str | None = None
    parameters: str | None = None
    number_lines: Any = None
    preserve_indent: bool | None = None
    retain_labels: bool | int | None = None
    use_labels: bool | None = None
    label_fmt: str | None = None


class ValueProperties(NodeProperties):
    """For nodes whose whole content is one verbatim string.

    example-block, fixed-width, code, verbatim, statistics-cookie.
    """

    value: str | None = None


class LatexProperties(NodeProperties):
    """A LaTeX fragment or environment, captured verbatim.

    Math is stored as source, never as a parsed expression tree.
    ``display`` and ``dialect`` are not part of ``ox-json``'s output --
    org-element distinguishes fragment from environment by node type
    alone.  They are here so the Emacs-free ingestion path, which has to
    synthesise these nodes out of plain text, can record what it detected.
    """

    value: str | None = None
    display: Literal["inline", "block"] | None = None
    dialect: str = "latex"


class LinkProperties(NodeProperties):
    type: str | None = None
    path: str | None = None
    raw_link: str | None = None
    format: str | None = None
    application: str | None = None
    search_option: str | None = None
    type_explicit_p: bool | None = None


class KeyValueProperties(NodeProperties):
    """A ``#+key: value`` keyword, or a ``:key: value`` drawer property."""

    key: str | None = None
    value: str | None = None


class DrawerProperties(NodeProperties):
    drawer_name: str | None = None


class PlainListProperties(NodeProperties):
    type: str | None = None  # ordered | unordered | descriptive


class ItemProperties(NodeProperties):
    bullet: str | None = None
    checkbox: str | None = None  # on | off | trans
    counter: str | int | None = None
    tag: list[OrgContent] | None = None
    pre_blank: int | None = None


class TableProperties(NodeProperties):
    type: str | None = None
    value: str | None = None
    tblfm: list[str] | None = None


class TableRowProperties(NodeProperties):
    type: str | None = None  # standard | rule


class TimestampNodeProperties(NodeProperties):
    raw_value: str | None = None
    #: active | inactive | active-range | inactive-range | diary
    type: str | None = None
    range_type: str | None = None  # timerange | daterange


class FootnoteDefinitionProperties(NodeProperties):
    label: str | None = None


class FootnoteReferenceProperties(NodeProperties):
    label: str | None = None
    type: str | None = None


class EntityProperties(NodeProperties):
    name: str | None = None
    latex: str | None = None
    latex_math_p: bool | None = None
    html: str | None = None
    ascii: str | None = None
    latin1: str | None = None
    utf_8: str | None = None
    use_brackets_p: bool | None = None


# -- nodes ------------------------------------------------------------------


class OrgNodeBase(_Model):
    """Common shape of every org node, and the class to isinstance against.

    :data:`OrgNode` is the typing alias naming the discriminated union;
    this is the runtime base every node class shares, so code that has to
    tell a node from the plain strings beside it in ``contents`` tests
    against this.

    ``contents`` is always a list here, even for the leaf nodes where
    ``ox-json`` omits the key entirely.  That is a deliberate difference:
    callers walking the tree should never have to distinguish "no
    children" from "cannot have children".
    """

    #: True when children are discrete -- rows, cells, list items -- rather
    #: than a continuous run of text.  Flattening a node's text has to space
    #: those apart instead of running them together, and keeping the answer
    #: on the class means registering a new node type carries its own text
    #: semantics rather than needing a name added to a list elsewhere.
    separates_children: ClassVar[bool] = False

    data_type: Literal["org-node"] = Field("org-node", alias="$$data_type")
    ref: str | None = None
    contents: Contents = Field(default_factory=list)


class GenericNode(OrgNodeBase):
    """Any node type without a dedicated class.

    Everything survives here -- the type string is kept as-is and the
    properties keep their raw keys -- so an unrecognised org construct is
    stored faithfully rather than rejected.
    """

    type: str
    properties: NodeProperties = Field(default_factory=NodeProperties)


class HeadlineNode(OrgNodeBase):
    type: Literal["headline"] = "headline"
    properties: HeadlineProperties = Field(default_factory=HeadlineProperties)
    #: Tags including those inherited from ancestors and ``#+filetags``.
    tags_all: list[str] = Field(default_factory=list)
    #: The headline's property drawer, flattened to a plain mapping.
    drawer: dict[str, Any] = Field(default_factory=dict)


class SectionNode(OrgNodeBase):
    type: Literal["section"] = "section"
    properties: NodeProperties = Field(default_factory=NodeProperties)


class ParagraphNode(OrgNodeBase):
    type: Literal["paragraph"] = "paragraph"
    properties: NodeProperties = Field(default_factory=NodeProperties)


class SrcBlockNode(OrgNodeBase):
    type: Literal["src-block"] = "src-block"
    properties: SrcBlockProperties = Field(default_factory=SrcBlockProperties)


class QuoteBlockNode(OrgNodeBase):
    type: Literal["quote-block"] = "quote-block"
    properties: NodeProperties = Field(default_factory=NodeProperties)


class ExampleBlockNode(OrgNodeBase):
    type: Literal["example-block"] = "example-block"
    properties: ValueProperties = Field(default_factory=ValueProperties)


class FixedWidthNode(OrgNodeBase):
    type: Literal["fixed-width"] = "fixed-width"
    properties: ValueProperties = Field(default_factory=ValueProperties)


class LinkNode(OrgNodeBase):
    type: Literal["link"] = "link"
    properties: LinkProperties = Field(default_factory=LinkProperties)
    is_internal: bool | None = None
    is_inline_image: bool | None = None
    #: Ref of the node this link resolves to, when it resolves in-document.
    target_ref: str | None = None


class TimestampNode(OrgNodeBase):
    """A timestamp appearing as content rather than as a planning value.

    Note the split: the resolved ``start``/``end`` sit on the node, the way
    a headline's ``tags-all`` does, while the kind and the source text sit
    in ``properties``.  A planning timestamp is shaped differently again --
    see :class:`Timestamp`, which is a value object with no node around it.
    """

    type: Literal["timestamp"] = "timestamp"
    properties: TimestampNodeProperties = Field(
        default_factory=TimestampNodeProperties
    )
    start: str | None = None
    end: str | None = None


class PropertyDrawerNode(OrgNodeBase):
    type: Literal["property-drawer"] = "property-drawer"
    properties: NodeProperties = Field(default_factory=NodeProperties)


class NodePropertyNode(OrgNodeBase):
    type: Literal["node-property"] = "node-property"
    properties: KeyValueProperties = Field(default_factory=KeyValueProperties)


class DrawerNode(OrgNodeBase):
    type: Literal["drawer"] = "drawer"
    properties: DrawerProperties = Field(default_factory=DrawerProperties)


class KeywordNode(OrgNodeBase):
    type: Literal["keyword"] = "keyword"
    properties: KeyValueProperties = Field(default_factory=KeyValueProperties)


class PlainListNode(OrgNodeBase):
    separates_children = True
    type: Literal["plain-list"] = "plain-list"
    properties: PlainListProperties = Field(default_factory=PlainListProperties)


class ItemNode(OrgNodeBase):
    type: Literal["item"] = "item"
    properties: ItemProperties = Field(default_factory=ItemProperties)


class TableNode(OrgNodeBase):
    separates_children = True
    type: Literal["table"] = "table"
    properties: TableProperties = Field(default_factory=TableProperties)


class TableRowNode(OrgNodeBase):
    separates_children = True
    type: Literal["table-row"] = "table-row"
    properties: TableRowProperties = Field(default_factory=TableRowProperties)


class TableCellNode(OrgNodeBase):
    type: Literal["table-cell"] = "table-cell"
    properties: NodeProperties = Field(default_factory=NodeProperties)


class LatexFragmentNode(OrgNodeBase):
    type: Literal["latex-fragment"] = "latex-fragment"
    properties: LatexProperties = Field(default_factory=LatexProperties)


class LatexEnvironmentNode(OrgNodeBase):
    type: Literal["latex-environment"] = "latex-environment"
    properties: LatexProperties = Field(default_factory=LatexProperties)


class FootnoteDefinitionNode(OrgNodeBase):
    type: Literal["footnote-definition"] = "footnote-definition"
    properties: FootnoteDefinitionProperties = Field(
        default_factory=FootnoteDefinitionProperties
    )


class FootnoteReferenceNode(OrgNodeBase):
    type: Literal["footnote-reference"] = "footnote-reference"
    properties: FootnoteReferenceProperties = Field(
        default_factory=FootnoteReferenceProperties
    )


class EntityNode(OrgNodeBase):
    type: Literal["entity"] = "entity"
    properties: EntityProperties = Field(default_factory=EntityProperties)


class StatisticsCookieNode(OrgNodeBase):
    type: Literal["statistics-cookie"] = "statistics-cookie"
    properties: ValueProperties = Field(default_factory=ValueProperties)


#: Node types with a dedicated class.  Everything else is a GenericNode.
_TYPED_NODES: dict[str, type[OrgNodeBase]] = {
    "drawer": DrawerNode,
    "entity": EntityNode,
    "example-block": ExampleBlockNode,
    "fixed-width": FixedWidthNode,
    "footnote-definition": FootnoteDefinitionNode,
    "footnote-reference": FootnoteReferenceNode,
    "headline": HeadlineNode,
    "item": ItemNode,
    "keyword": KeywordNode,
    "latex-environment": LatexEnvironmentNode,
    "latex-fragment": LatexFragmentNode,
    "link": LinkNode,
    "node-property": NodePropertyNode,
    "paragraph": ParagraphNode,
    "plain-list": PlainListNode,
    "property-drawer": PropertyDrawerNode,
    "quote-block": QuoteBlockNode,
    "section": SectionNode,
    "src-block": SrcBlockNode,
    "statistics-cookie": StatisticsCookieNode,
    "table": TableNode,
    "table-cell": TableCellNode,
    "table-row": TableRowNode,
    "timestamp": TimestampNode,
}

_GENERIC_TAG = "*"


def node_types() -> list[str]:
    """The node types with a dedicated model class, sorted."""
    return sorted(_TYPED_NODES)


def _node_tag(value: Any) -> str:
    """Route a node to its class by ``type``, or to :class:`GenericNode`."""
    if isinstance(value, OrgNodeBase):
        kind = getattr(value, "type", None)
    elif isinstance(value, dict):
        kind = value.get("type")
    else:
        return _GENERIC_TAG
    return kind if kind in _TYPED_NODES else _GENERIC_TAG


_NODE_UNION = Union[
    tuple(Annotated[cls, Tag(kind)] for kind, cls in _TYPED_NODES.items())
    + (Annotated[GenericNode, Tag(_GENERIC_TAG)],)
]

# These are PEP 695 aliases rather than plain assignments on purpose: the
# union is recursive, and only a *named* alias gives pydantic something to
# emit a single `$ref` to.  Assigning the bare `Annotated[...]` makes it
# re-expand the whole union at every nesting level and never terminate.
type OrgNode = Annotated[_NODE_UNION, Discriminator(_node_tag)]

#: One entry of ``contents``: a child node, or a run of plain text.
type OrgContent = Union[str, OrgNode]


def _empty_if_null(value: Any) -> Any:
    """``ox-json`` omits ``contents`` on leaves; read that as no children."""
    return [] if value is None else value


#: A node's or document's children, normalised so absent means empty.
type Contents = Annotated[list[OrgContent], BeforeValidator(_empty_if_null)]


# -- document ---------------------------------------------------------------


class DocumentProperties(_Model):
    """File-level keywords, as ``ox-json`` collects them.

    The parsed ones (``title``, ``author``, ``date``, ``description``) are
    secondary strings -- lists that may contain markup nodes, not plain
    strings -- so a bare string is promoted to a one-element list rather
    than rejected.  ``filetags`` arrives already split into a list, which
    the Emacs-free ingestion path has to reproduce from a raw
    ``:tag1:tag2:`` string.
    """

    title: list[OrgContent] = Field(default_factory=list)
    author: list[OrgContent] = Field(default_factory=list)
    date: list[OrgContent] = Field(default_factory=list)
    description: list[OrgContent] = Field(default_factory=list)
    filetags: list[str] = Field(default_factory=list)
    creator: str | None = None
    email: str | None = None
    language: str | None = None

    @field_validator(
        "title", "author", "date", "description", "filetags", mode="before"
    )
    @classmethod
    def _listify(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value


class OrgDocument(_Model):
    """A whole org document: the root of the canonical representation."""

    data_type: Literal["org-document"] = Field(
        "org-document", alias="$$data_type"
    )
    #: Which revision of this module produced the stored JSON.
    schema_version: int = SCHEMA_VERSION
    properties: DocumentProperties = Field(default_factory=DocumentProperties)
    contents: Contents = Field(default_factory=list)

    def dump(self) -> dict[str, Any]:
        """The canonical JSON-ready dict, using wire names for every key."""
        return self.model_dump(mode="json", by_alias=True)


def walk(root: OrgDocument | OrgNodeBase) -> Iterator[OrgNodeBase]:
    """Yield every node under ROOT, depth first, parents before children.

    Only ``contents`` is traversed.  Nodes embedded in properties -- a
    headline's ``title``, an item's ``tag`` -- are not content and are
    left to callers that care about them.
    """
    for child in root.contents:
        if isinstance(child, str):
            continue
        yield child
        yield from walk(child)


# Resolve the forward references the recursive union needs.
for _cls in (
    *_TYPED_NODES.values(),
    GenericNode,
    HeadlineProperties,
    ItemProperties,
    DocumentProperties,
    OrgDocument,
):
    _cls.model_rebuild()
del _cls
