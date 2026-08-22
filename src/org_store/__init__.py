# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""A store for org content, abstracted over several backends.

The canonical representation is :mod:`org_store.model`: a versioned,
schema-validated JSON document shaped like ``ox-json``'s export.  Every
ingestion path -- Emacs/``ox-json``, or ``org-parser`` without Emacs --
produces that same shape, so everything above it (storage, search, sync,
and the CLI/MCP adapters) is written once against one model.

This package deliberately does not import :mod:`org_cli_helper`; it is a
library the CLI uses, not the other way round.
"""

from __future__ import annotations

from .model import (
    SCHEMA_VERSION,
    DocumentProperties,
    OrgDocument,
    OrgNode,
    OrgNodeBase,
    node_types,
    walk,
)
from .refs import assign_refs, compute_ref
from .store import DocId, StoreError
from .text import node_text, plain_text
from .tree import iter_locations

__all__ = [
    "SCHEMA_VERSION",
    "DocId",
    "DocumentProperties",
    "OrgDocument",
    "OrgNode",
    "OrgNodeBase",
    "StoreError",
    "assign_refs",
    "compute_ref",
    "iter_locations",
    "node_text",
    "node_types",
    "plain_text",
    "walk",
]
