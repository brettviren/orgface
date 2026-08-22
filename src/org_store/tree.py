# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Finding and editing nodes inside a document.

One traversal serves everything above it: :mod:`org_store.refs` uses it to
assign refs, :mod:`org_store.extract` to derive index rows, and the
service layer to address a single node for update or deletion.  Keeping
that walk in one place is what makes the ref of a node, the row indexed
for it, and the slot edited when it changes all agree by construction.

A :class:`Location` is a node plus enough context to write it back --
where it lives and under what key -- so callers never re-derive that.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from .model import OrgDocument, OrgNodeBase
from .store import StoreError

#: A node's position: ``contents`` indices interleaved with property names.
NodePath = tuple[int | str, ...]


class TreeError(StoreError):
    """A node could not be addressed or edited as asked.

    A :class:`~org_store.store.StoreError` so that adapters need catch
    only the one base class: how a node failed to be located is an
    internal detail, not something a CLI should have to know about.
    """


@dataclass(frozen=True)
class Location:
    """Where one node sits, and how to change it.

    ``container``/``key`` name the slot holding the node.  Nodes usually
    live in a list -- a parent's ``contents``, or a list-valued property
    such as a headline's ``title`` -- but a property may hold one
    directly, so mappings and model attributes are supported too.
    """

    path: NodePath
    node: OrgNodeBase
    parent: OrgNodeBase | None
    container: Any
    key: int | str

    def replace(self, node: OrgNodeBase) -> None:
        """Put NODE in this slot, in place."""
        if isinstance(self.container, (list, dict)):
            self.container[self.key] = node
        elif isinstance(self.container, BaseModel):
            setattr(self.container, str(self.key), node)
        else:  # pragma: no cover - traversal yields nothing else
            raise TreeError(f"cannot write into {type(self.container).__name__}")

    def remove(self) -> None:
        """Take this node out.

        Removing from a list closes the gap, which shifts every later
        sibling; from a mapping or an attribute it leaves a hole instead,
        since those positions are named rather than ordered.
        """
        if isinstance(self.container, (list, dict)):
            del self.container[self.key]
        elif isinstance(self.container, BaseModel):
            setattr(self.container, str(self.key), None)
        else:  # pragma: no cover - traversal yields nothing else
            raise TreeError(f"cannot delete from {type(self.container).__name__}")


def iter_locations(doc: OrgDocument) -> Iterator[Location]:
    """Yield every node in DOC, parents before children.

    Properties are walked as well as ``contents``: nodes hide in a
    headline's ``title``, an item's ``tag``, and whatever an unrecognised
    node type carries.  Anything that needs a ref, an index row, or an
    edit has to be reachable, so everything is reached.

    Mappings are visited in sorted key order rather than insertion order.
    Extra properties arrive in whatever order the source engine emitted
    them, and a path -- hence a ref -- that depended on that would not be
    stable.
    """
    yield from _visit(doc.properties, ("properties",), None, doc, "properties")
    for index, item in enumerate(doc.contents):
        yield from _visit(item, (index,), None, doc.contents, index)


def find(doc: OrgDocument, ref: str) -> Location | None:
    """The location of the node REF names, or None."""
    return next((spot for spot in iter_locations(doc) if spot.node.ref == ref), None)


def locate(doc: OrgDocument, ref: str) -> Location:
    """The location of the node REF names, or raise :class:`TreeError`."""
    spot = find(doc, ref)
    if spot is None:
        raise TreeError(f"no node with ref {ref!r}")
    return spot


def _visit(
    value: Any,
    path: NodePath,
    parent: OrgNodeBase | None,
    container: Any,
    key: int | str,
) -> Iterator[Location]:
    if isinstance(value, OrgNodeBase):
        yield Location(path, value, parent, container, key)
        yield from _visit(
            value.properties, (*path, "properties"), value, value, "properties"
        )
        # Children extend the parent's path directly, so a path reads as
        # the chain of child indices from the document root.
        for index, item in enumerate(value.contents):
            yield from _visit(item, (*path, index), value, value.contents, index)
    elif isinstance(value, BaseModel):
        extra = value.__pydantic_extra__ or {}
        for name in sorted({*type(value).model_fields, *extra}):
            yield from _visit(
                getattr(value, name, None), (*path, name), parent, value, name
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _visit(item, (*path, index), parent, value, index)
    elif isinstance(value, dict):
        for name in sorted(value, key=str):
            yield from _visit(value[name], (*path, str(name)), parent, value, name)
