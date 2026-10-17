# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Copying documents from one store to another.

One direction, explicitly asked for, no conflict resolution.  What this
does is exactly what it says: for every document in the source, make the
destination's copy match it.  Where the two differ, the source wins --
including where the difference is a change somebody made on the
destination side.

That is the whole of it, and it is worth being blunt about the limit:
comparing two hashes tells you the documents differ, not *who* changed.
Detecting that both sides moved since some earlier point needs a record
of that earlier point and a three-way merge, and inventing either here
would produce something that looked safe without being it.  So this is a
copy, in the direction you name, and the CLI says so.

Sync goes through the service layer rather than reaching into backends,
so the destination gets validated documents with its own refs assigned
and its own indexes rebuilt, exactly as if they had been written by hand.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from .service import OrgStore
from .store import DocId, DocumentNotFound

log = logging.getLogger(__name__)


class Outcome(str, Enum):
    """What happened to one document."""

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    DELETED = "deleted"
    FAILED = "failed"


@dataclass(frozen=True)
class Change:
    """One document's part in a sync."""

    doc_id: DocId
    outcome: Outcome
    detail: str = ""


@dataclass
class SyncReport:
    """Everything a sync did, or would have done."""

    changes: list[Change] = field(default_factory=list)
    dry_run: bool = False

    def count(self, outcome: Outcome) -> int:
        return sum(1 for change in self.changes if change.outcome is outcome)

    @property
    def written(self) -> int:
        """How many documents were changed at the destination."""
        return sum(
            self.count(outcome)
            for outcome in (Outcome.CREATED, Outcome.UPDATED, Outcome.DELETED)
        )

    @property
    def failed(self) -> list[Change]:
        return [c for c in self.changes if c.outcome is Outcome.FAILED]


def sync(
    source: OrgStore,
    dest: OrgStore,
    doc_ids: Iterable[DocId] | None = None,
    *,
    prune: bool = False,
    dry_run: bool = False,
) -> SyncReport:
    """Make DEST's copy of each of SOURCE's documents match.

    DOC_IDS limits the run to some of the source's documents; by default
    every one of them is considered.

    ``prune`` also deletes documents the destination has and the source
    does not.  Off by default, and never on for a partial run: with
    DOC_IDS given, "not in the source" cannot be distinguished from "not
    asked about".

    ``dry_run`` reports what would happen and writes nothing.

    A document that fails is recorded and the run continues.  One
    unreadable file should not strand the rest half-copied.
    """
    report = SyncReport(dry_run=dry_run)
    wanted = list(doc_ids) if doc_ids is not None else list(source.list())
    seen = set(wanted)

    log.debug("syncing %d document(s)%s", len(wanted), " (dry run)" if dry_run else "")
    for doc_id in wanted:
        change = _one(source, dest, doc_id, dry_run=dry_run)
        log.debug("%s: %s %s", doc_id, change.outcome.value, change.detail or "")
        report.changes.append(change)

    if prune and doc_ids is None:
        for doc_id in dest.list():
            if doc_id not in seen:
                report.changes.append(_prune(dest, doc_id, dry_run=dry_run))

    return report


def _one(
    source: OrgStore, dest: OrgStore, doc_id: DocId, *, dry_run: bool
) -> Change:
    """Bring one document across, if it needs to come."""
    try:
        incoming = source.hash(doc_id)
    except Exception as exc:  # noqa: BLE001 - one bad document must not stop the run
        return Change(doc_id, Outcome.FAILED, f"cannot read from source: {exc}")

    try:
        existing = dest.hash(doc_id)
    except DocumentNotFound:
        existing = None
    except Exception as exc:  # noqa: BLE001
        return Change(doc_id, Outcome.FAILED, f"cannot read from destination: {exc}")

    if existing == incoming:
        return Change(doc_id, Outcome.UNCHANGED)

    try:
        doc = source.read(doc_id)
        if existing is None:
            if not dry_run:
                dest.create(doc, path_hint=doc_id)
            return Change(doc_id, Outcome.CREATED)
        if not dry_run:
            dest.replace(doc_id, doc)
        return Change(doc_id, Outcome.UPDATED)
    except Exception as exc:  # noqa: BLE001
        return Change(doc_id, Outcome.FAILED, str(exc))


def _prune(dest: OrgStore, doc_id: DocId, *, dry_run: bool) -> Change:
    try:
        if not dry_run:
            dest.delete(doc_id)
        return Change(doc_id, Outcome.DELETED)
    except Exception as exc:  # noqa: BLE001
        return Change(doc_id, Outcome.FAILED, str(exc))
