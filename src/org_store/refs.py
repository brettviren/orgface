# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Stable identifiers for nodes within a document.

A ref names one node so that CRUD operations can address part of a
document rather than the whole of it.  It is computed here, in Python, as
a pure function of the document id and the node's structural path -- never
taken from whichever engine happened to parse the document.

That is a deliberate choice.  ``ox-json`` can emit its own refs, and with
``:json-deterministic-refs t`` they are stable across exports (without it
they are regenerated every export and useless for addressing).  But they
would still only ever agree with themselves: the Emacs-free ingestion path
has no way to reproduce Emacs' scheme.  Computing refs on this side means
both paths yield the same ref for the same node of the same document, by
construction, which is what lets a document be synced between backends and
stay addressable the same way.

A ref names a position in a structure, not an object with a lifetime, so
inserting or deleting a node changes the refs of its later siblings and
their descendants.  Callers holding a ref across an edit must re-read it;
:mod:`org_store.service` reassigns refs after every write for exactly this
reason.  The identifier that does survive edits is the one org already
has, the ``ID`` property, which lives in the node's own properties.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from .model import OrgDocument
from .tree import iter_locations

#: Marks a string as one of ours, and keeps refs valid SQL identifiers.
REF_PREFIX = "org"

#: Hex digits of digest kept.  Refs are the primary key across a whole
#: corpus rather than one document, so this is sized against the birthday
#: bound: 64 bits leaves collisions negligible for any plausible number of
#: nodes, where ``ox-json``'s own seven digits would not.
REF_DIGITS = 16


def compute_ref(doc_id: str, path: Sequence[int | str]) -> str:
    """The ref of the node at PATH within the document named DOC_ID.

    The separator cannot occur in a path element, so distinct paths hash
    distinct strings: ``("a", "b")`` and ``("a/b",)`` do not collide.
    """
    key = "\x00".join([doc_id, *(str(part) for part in path)])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return REF_PREFIX + digest[:REF_DIGITS]


def assign_refs(
    doc: OrgDocument, doc_id: str, *, copy: bool = True
) -> OrgDocument:
    """Return DOC with every node's ref recomputed.

    Call this at the boundary of every ingestion path, so whatever refs
    the source engine supplied are replaced by ours before the document is
    stored or addressed.

    A deep copy is taken by default, since a caller that handed in a
    document it still holds should not see it mutated.  Pass
    ``copy=False`` where the document was just read and is about to be
    written back -- on a large document the copy costs more than the walk.
    """
    fresh = doc.model_copy(deep=True) if copy else doc
    for spot in list(iter_locations(fresh)):
        spot.node.ref = compute_ref(doc_id, spot.path)
    return fresh
