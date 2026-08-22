# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Deriving index rows from a document.

The canonical JSON is the source of truth; everything here is a view onto
it, computed fresh on every write.  No row produced here is ever edited
independently, so a change to this module is repaired by reindexing rather
than by migrating data.

Kept out of the sqlite backend on purpose: what counts as a tag, a
property, or a link is a fact about org, not about how rows are stored, so
any future backend that wants the same indexes gets them from here rather
than reimplementing the rules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .model import OrgDocument, Timestamp
from .text import heading_text, own_text
from .tree import iter_locations

#: Property keys that hold a planning timestamp on a headline.
PLANNING = ("scheduled", "deadline", "closed")


@dataclass(frozen=True)
class NodeRow:
    ref: str
    doc_id: str
    type: str
    parent_ref: str | None
    path_json: str
    heading_level: int | None
    todo_keyword: str | None
    priority: str | None


@dataclass(frozen=True)
class TagRow:
    ref: str
    doc_id: str
    tag: str


@dataclass(frozen=True)
class PropertyRow:
    ref: str
    doc_id: str
    key: str
    value: str


@dataclass(frozen=True)
class LinkRow:
    ref: str
    doc_id: str
    target: str
    link_type: str | None


@dataclass(frozen=True)
class TimestampRow:
    ref: str
    doc_id: str
    kind: str
    start: str | None
    end: str | None


@dataclass(frozen=True)
class FtsRow:
    """One node's searchable text.

    Heading and body are separate columns so a query can be scoped to one
    or the other, which is what makes "find the heading about X" a
    different question from "find a note that mentions X".
    """

    ref: str
    doc_id: str
    heading_text: str
    body_text: str


@dataclass
class Index:
    """Everything derived from one document, ready to be written.

    Each attribute is named for the table it fills.
    """

    nodes: list[NodeRow] = field(default_factory=list)
    tags: list[TagRow] = field(default_factory=list)
    properties: list[PropertyRow] = field(default_factory=list)
    links: list[LinkRow] = field(default_factory=list)
    timestamps: list[TimestampRow] = field(default_factory=list)
    fts_nodes: list[FtsRow] = field(default_factory=list)


def content_hash(doc: OrgDocument) -> str:
    """A hash of DOC's canonical content, for change detection and sync.

    Deliberately a hash of the canonical JSON rather than of any file the
    document came from.  Sync compares hashes taken from two different
    backends, so the two must mean the same thing; a file-digest would be
    cheaper for a file-backed store but could never equal a hash taken
    from sqlite, which would make every comparison across backends read as
    "changed".

    Refs are excluded.  They are a function of the document id, so two
    stores holding the same content under different ids would otherwise
    look different when they are not.
    """
    return hash_dump(doc.dump())


def hash_dump(payload: dict[str, Any]) -> str:
    """As :func:`content_hash`, for a caller that already has the dump.

    Writing a document serialises it once for storage; making it dump a
    second time just to hash it doubles the most expensive part of a
    write.  PAYLOAD is consumed -- its refs are stripped in place.
    """
    _strip_refs(payload)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def index_document(doc_id: str, doc: OrgDocument) -> Index:
    """Derive every index row for DOC.

    Nodes without a ref are skipped rather than guessed at: a ref is what
    a row is keyed by, and :func:`org_store.refs.assign_refs` runs before
    anything reaches here.
    """
    index = Index()
    for spot in iter_locations(doc):
        node = spot.node
        if node.ref is None:
            continue
        index.nodes.append(
            NodeRow(
                ref=node.ref,
                doc_id=doc_id,
                type=node.type,
                parent_ref=spot.parent.ref if spot.parent else None,
                path_json=json.dumps(list(spot.path)),
                heading_level=node.properties.get("level"),
                todo_keyword=node.properties.get("todo_keyword"),
                priority=_string(node.properties.get("priority")),
            )
        )
        _index_text(index, doc_id, node)
        _index_tags(index, doc_id, node)
        _index_properties(index, doc_id, node)
        _index_links(index, doc_id, node)
        _index_timestamps(index, doc_id, node)
    return index


# -- per-node rules ---------------------------------------------------------


def _index_text(index: Index, doc_id: str, node: Any) -> None:
    """Index a node's own searchable text, if it has any."""
    heading = heading_text(node) if node.type == "headline" else ""
    body = own_text(node)
    if heading.strip() or body.strip():
        index.fts_nodes.append(FtsRow(node.ref, doc_id, heading, body))


def _index_tags(index: Index, doc_id: str, node: Any) -> None:
    """Index a headline's effective tags.

    ``tags-all`` is preferred over ``tags`` because it includes the ones
    inherited from ancestor headlines and from ``#+filetags``, which is
    what someone searching for a tag means by it.  Documents whose source
    engine does not compute inheritance fall back to the local tags.
    """
    inherited = getattr(node, "tags_all", None)
    local = node.properties.get("tags")
    for tag in inherited or local or ():
        if isinstance(tag, str) and tag:
            index.tags.append(TagRow(node.ref, doc_id, tag))


def _index_properties(index: Index, doc_id: str, node: Any) -> None:
    """Index a headline's property drawer, and standalone node-properties."""
    drawer = getattr(node, "drawer", None)
    if isinstance(drawer, dict):
        for key, value in drawer.items():
            index.properties.append(
                PropertyRow(node.ref, doc_id, str(key), _string(value) or "")
            )
    if node.type == "node-property":
        key = node.properties.get("key")
        if key:
            value = _string(node.properties.get("value")) or ""
            index.properties.append(PropertyRow(node.ref, doc_id, str(key), value))


def _index_links(index: Index, doc_id: str, node: Any) -> None:
    if node.type != "link":
        return
    # raw-link is the whole target as written; path drops the scheme.
    target = node.properties.get("raw_link") or node.properties.get("path")
    if target:
        index.links.append(
            LinkRow(node.ref, doc_id, str(target), node.properties.get("type"))
        )


def _index_timestamps(index: Index, doc_id: str, node: Any) -> None:
    """Index planning timestamps and standalone timestamp nodes.

    The two are shaped differently: planning values are :class:`Timestamp`
    value objects inside ``properties``, keyed by which planning line they
    came from, while a timestamp node keeps ``start``/``end`` on the node
    and its kind in ``properties``.
    """
    for kind in PLANNING:
        stamp = node.properties.get(kind)
        start, end = _stamp_bounds(stamp)
        if start is not None or end is not None:
            index.timestamps.append(TimestampRow(node.ref, doc_id, kind, start, end))

    if node.type == "timestamp":
        kind = node.properties.get("type") or "timestamp"
        index.timestamps.append(
            TimestampRow(
                node.ref,
                doc_id,
                str(kind),
                _string(getattr(node, "start", None)),
                _string(getattr(node, "end", None)),
            )
        )


def _stamp_bounds(stamp: Any) -> tuple[str | None, str | None]:
    """Read start/end off a planning timestamp, typed or still raw."""
    if isinstance(stamp, Timestamp):
        return _string(stamp.start), _string(stamp.end)
    if isinstance(stamp, dict) and stamp.get("$$data_type") == "timestamp":
        return _string(stamp.get("start")), _string(stamp.get("end"))
    return None, None


# -- helpers ----------------------------------------------------------------


def _string(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)


def _strip_refs(value: Any) -> None:
    if isinstance(value, dict):
        value.pop("ref", None)
        for item in value.values():
            _strip_refs(item)
    elif isinstance(value, list):
        for item in value:
            _strip_refs(item)
