# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""A store backend keeping documents in one sqlite file.

The canonical JSON in ``documents.content_json`` is the source of truth.
Every other table is an index derived from it by :func:`_reindex` on each
write, never written to on its own.  That means the side tables can be
dropped and rebuilt from the documents table at any time with no data
loss, which is the whole reason to treat them as indexes rather than as a
second, subtly-diverging copy of the content.

The indexed set answers the queries the store is meant to serve -- find by
tag, by property, by link target, by schedule -- and stops there.
Anything else goes through sqlite's JSON1 functions against
``content_json`` directly; a query pattern earns a table only once it is
common enough to want an index for.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import astuple, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..extract import Index, hash_dump, index_document
from ..model import OrgDocument
from ..search import SearchQuery, SearchResult, match_expression
from ..store import DocId, DocumentExists, DocumentNotFound

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id         TEXT PRIMARY KEY,
    path           TEXT,
    schema_version INTEGER NOT NULL,
    content_json   TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- Keyed by (doc_id, ref) rather than by ref alone.  A ref already hashes
-- in the document id, so refs assigned by the service are unique across
-- the corpus anyway -- but that is an invariant enforced two layers up,
-- and a table should not corrupt one document because a caller reached
-- past the service and stored another with refs it had not reassigned.
CREATE TABLE IF NOT EXISTS nodes (
    doc_id        TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    ref           TEXT NOT NULL,
    type          TEXT NOT NULL,
    parent_ref    TEXT,
    path_json     TEXT NOT NULL,
    heading_level INTEGER,
    todo_keyword  TEXT,
    priority      TEXT,
    PRIMARY KEY (doc_id, ref)
);

CREATE TABLE IF NOT EXISTS tags (
    doc_id TEXT NOT NULL,
    ref    TEXT NOT NULL,
    tag    TEXT NOT NULL,
    FOREIGN KEY (doc_id, ref) REFERENCES nodes(doc_id, ref) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS properties (
    doc_id TEXT NOT NULL,
    ref    TEXT NOT NULL,
    key    TEXT NOT NULL,
    value  TEXT NOT NULL,
    FOREIGN KEY (doc_id, ref) REFERENCES nodes(doc_id, ref) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS links (
    doc_id    TEXT NOT NULL,
    ref       TEXT NOT NULL,
    target    TEXT NOT NULL,
    link_type TEXT,
    FOREIGN KEY (doc_id, ref) REFERENCES nodes(doc_id, ref) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS timestamps (
    doc_id TEXT NOT NULL,
    ref    TEXT NOT NULL,
    kind   TEXT NOT NULL,
    start  TEXT,
    end    TEXT,
    FOREIGN KEY (doc_id, ref) REFERENCES nodes(doc_id, ref) ON DELETE CASCADE
);

-- Not a contentless FTS5 table: it stores the text so that snippet()
-- can cut an excerpt out of it.  A contentless table would be smaller,
-- but every result would have to be rendered by re-reading and
-- re-flattening its document, which costs far more than the text does.
CREATE VIRTUAL TABLE IF NOT EXISTS fts_nodes USING fts5(
    ref UNINDEXED,
    doc_id UNINDEXED,
    heading_text,
    body_text,
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE INDEX IF NOT EXISTS nodes_by_type ON nodes(type);
CREATE INDEX IF NOT EXISTS nodes_by_parent ON nodes(parent_ref);
CREATE INDEX IF NOT EXISTS tags_by_tag ON tags(tag);
CREATE INDEX IF NOT EXISTS tags_by_doc ON tags(doc_id);
CREATE INDEX IF NOT EXISTS properties_by_key ON properties(key, value);
CREATE INDEX IF NOT EXISTS properties_by_doc ON properties(doc_id);
CREATE INDEX IF NOT EXISTS links_by_target ON links(target);
CREATE INDEX IF NOT EXISTS links_by_doc ON links(doc_id);
CREATE INDEX IF NOT EXISTS timestamps_by_kind ON timestamps(kind, start);
CREATE INDEX IF NOT EXISTS timestamps_by_doc ON timestamps(doc_id);
"""

#: Side tables, in the order they are cleared.  Every one is derived.
#: ``fts_nodes`` is a virtual table and so cannot carry a foreign key --
#: it is cleared explicitly rather than by cascade, which is why deleting
#: a document clears the derived rows itself instead of relying on one.
DERIVED = ("fts_nodes", "timestamps", "links", "properties", "tags", "nodes")

#: Column positions in ``fts_nodes``, for bm25() and snippet().
_FTS_HEADING, _FTS_BODY = 2, 3

#: bm25 weights per column.  A hit in a heading outranks one in a body:
#: somebody who titled a note about a thing meant it to be about that
#: thing, where a passing mention in prose is weaker evidence.
_FTS_WEIGHTS = (0.0, 0.0, 10.0, 1.0)


class SqliteStore:
    """Documents in a sqlite database.

    Pass ``":memory:"`` for a throwaway store; anything else is a file
    path, whose parent directories are created on demand.
    """

    def __init__(self, database: Path | str) -> None:
        self.database = database
        if database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(database))
        self._db.row_factory = sqlite3.Row
        # Without this the ON DELETE CASCADE above is silently inert.
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- documents ----------------------------------------------------------

    def allocate_id(self, path_hint: str | None = None) -> DocId:
        return path_hint or uuid.uuid4().hex

    def create_document(
        self, doc: OrgDocument, *, path_hint: str | None = None
    ) -> DocId:
        doc_id = self.allocate_id(path_hint)
        if self._exists(doc_id):
            raise DocumentExists(doc_id)
        self._write(doc_id, doc, path=path_hint)
        return doc_id

    def read_document(self, doc_id: DocId) -> OrgDocument:
        row = self._db.execute(
            "SELECT content_json FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            raise DocumentNotFound(doc_id)
        return OrgDocument.model_validate(json.loads(row["content_json"]))

    def replace_document(self, doc_id: DocId, doc: OrgDocument) -> None:
        row = self._db.execute(
            "SELECT path FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            raise DocumentNotFound(doc_id)
        self._write(doc_id, doc, path=row["path"])

    def delete_document(self, doc_id: DocId) -> None:
        if not self._exists(doc_id):
            raise DocumentNotFound(doc_id)
        with self._db:
            # Explicitly rather than by cascade: fts_nodes is virtual and
            # cannot carry a foreign key, so a cascade would leave its rows
            # behind to be returned by later searches.
            self._clear_derived(doc_id)
            self._db.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))

    def list_documents(self) -> Iterable[DocId]:
        rows = self._db.execute("SELECT doc_id FROM documents ORDER BY doc_id")
        return [row["doc_id"] for row in rows]

    def content_hash(self, doc_id: DocId) -> str:
        row = self._db.execute(
            "SELECT content_hash FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            raise DocumentNotFound(doc_id)
        return row["content_hash"]

    # -- queries over the derived tables ------------------------------------

    def refs_with_tag(self, tag: str) -> list[tuple[DocId, str]]:
        rows = self._db.execute(
            "SELECT doc_id, ref FROM tags WHERE tag = ? ORDER BY doc_id, ref",
            (tag,),
        )
        return [(row["doc_id"], row["ref"]) for row in rows]

    def refs_with_property(self, key: str, value: str | None = None):
        sql = "SELECT doc_id, ref FROM properties WHERE key = ?"
        params: list[str] = [key]
        if value is not None:
            sql += " AND value = ?"
            params.append(value)
        rows = self._db.execute(sql + " ORDER BY doc_id, ref", params)
        return [(row["doc_id"], row["ref"]) for row in rows]

    def links_to(self, target: str) -> list[tuple[DocId, str]]:
        rows = self._db.execute(
            "SELECT doc_id, ref FROM links WHERE target = ? ORDER BY doc_id, ref",
            (target,),
        )
        return [(row["doc_id"], row["ref"]) for row in rows]

    def search(self, query: SearchQuery) -> list[SearchResult]:
        """Answer a full-text query against ``fts_nodes``.

        Everything that is not about text -- document, node type, tags,
        properties -- is applied against the side tables rather than
        folded into the FTS expression, so each qualifier is an index
        lookup and they compose freely.
        """
        expression = match_expression(query.text or "", query.scope)
        if not expression:
            return []

        # Tag and property filters cover whole subtrees, so they are
        # recursive CTEs.  Those come first in the statement, so their
        # parameters must be bound before the MATCH expression's.
        ctes: list[str] = []
        conditions: list[str] = []
        params: list[Any] = []

        if query.tags:
            ctes.append(
                _subtree(
                    "tagged",
                    "SELECT doc_id, ref FROM tags"
                    f" WHERE tag IN ({_marks(query.tags)})",
                )
            )
            conditions.append(_within("tagged"))
            params.extend(query.tags)

        for number, (key, value) in enumerate(
            (query.property_filters or {}).items()
        ):
            name = f"prop{number}"
            ctes.append(
                _subtree(
                    name,
                    "SELECT doc_id, ref FROM properties"
                    " WHERE key = ? AND value = ?",
                )
            )
            conditions.append(_within(name))
            params.extend([key, value])

        sql: list[str] = []
        if ctes:
            sql.append("WITH RECURSIVE " + ",\n".join(ctes))
        weights = ", ".join(str(weight) for weight in _FTS_WEIGHTS)
        sql += [
            "SELECT f.ref AS ref, f.doc_id AS doc_id, n.type AS type,",
            f"       bm25(fts_nodes, {weights}) AS rank_score,",
            f"       snippet(fts_nodes, {_FTS_HEADING}, '', '', '', 12)"
            " AS heading_snippet,",
            f"       snippet(fts_nodes, {_FTS_BODY}, '', '', '...', 16)"
            " AS body_snippet",
            "FROM fts_nodes f",
            "JOIN nodes n ON n.doc_id = f.doc_id AND n.ref = f.ref",
            "WHERE fts_nodes MATCH ?",
        ]
        params.append(expression)

        if query.doc_ids:
            sql.append(f"AND f.doc_id IN ({_marks(query.doc_ids)})")
            params.extend(query.doc_ids)
        if query.node_types:
            sql.append(f"AND n.type IN ({_marks(query.node_types)})")
            params.extend(query.node_types)
        sql.extend(conditions)

        sql.append("ORDER BY rank_score LIMIT ?")
        params.append(query.top_k)

        rows = self._db.execute("\n".join(sql), params)
        return [
            SearchResult(
                doc_id=row["doc_id"],
                ref=row["ref"],
                type=row["type"],
                # bm25 counts down from zero, best first.  Flipped here so
                # a result's score reads the way a score should.
                score=-row["rank_score"],
                excerpt=row["body_snippet"] or row["heading_snippet"] or "",
            )
            for row in rows
        ]

    def counts(self, doc_id: DocId) -> dict[str, int]:
        """How many rows each derived table holds for one document."""
        if not self._exists(doc_id):
            raise DocumentNotFound(doc_id)
        return {
            table: self._db.execute(
                f"SELECT count(*) AS n FROM {table} WHERE doc_id = ?", (doc_id,)
            ).fetchone()["n"]
            for table in DERIVED
        }

    def reindex_all(self) -> int:
        """Rebuild every derived table from the stored documents.

        Nothing is lost by doing this, which is the point of the design:
        after a change to the extraction rules, this is the whole
        migration.
        """
        count = 0
        with self._db:
            # Everything is being rebuilt, so clear each table once rather
            # than issuing a per-document delete for every one of them.
            for table in DERIVED:
                self._db.execute(f"DELETE FROM {table}")
            # A separate cursor: the inserts below run on the connection,
            # and this one is streamed rather than read into memory whole.
            documents = self._db.cursor().execute(
                "SELECT doc_id, content_json FROM documents"
            )
            for row in documents:
                doc = OrgDocument.model_validate(json.loads(row["content_json"]))
                self._insert_index(index_document(row["doc_id"], doc))
                count += 1
        return count

    # -- internals ----------------------------------------------------------

    def _exists(self, doc_id: DocId) -> bool:
        found = self._db.execute(
            "SELECT 1 FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return found is not None

    def _write(self, doc_id: DocId, doc: OrgDocument, path: str | None) -> None:
        """Store DOC and rebuild its derived rows, as one transaction."""
        dumped = doc.dump()
        payload = json.dumps(dumped, sort_keys=True)
        # hash_dump consumes the dict, so it must come after the payload.
        digest = hash_dump(dumped)
        with self._db:  # commits on success, rolls back on error
            self._db.execute(
                "INSERT INTO documents"
                " (doc_id, path, schema_version, content_json, content_hash,"
                "  updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(doc_id) DO UPDATE SET"
                "  path = excluded.path,"
                "  schema_version = excluded.schema_version,"
                "  content_json = excluded.content_json,"
                "  content_hash = excluded.content_hash,"
                "  updated_at = excluded.updated_at",
                (
                    doc_id,
                    path,
                    doc.schema_version,
                    payload,
                    digest,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            self._clear_derived(doc_id)
            self._insert_index(index_document(doc_id, doc))

    def _clear_derived(self, doc_id: DocId) -> None:
        for table in DERIVED:
            self._db.execute(f"DELETE FROM {table} WHERE doc_id = ?", (doc_id,))

    def _insert_index(self, index: Index) -> None:
        """Write every derived row.

        Driven by the dataclasses rather than by five hand-written
        statements: each row type's field names are its table's column
        names, in order, and each ``Index`` attribute is named for its
        table.  A new index table therefore needs no code here.
        """
        # DERIVED is in delete order, children first; inserting has to run
        # the other way or a side row arrives before the node it references.
        for table in reversed(DERIVED):
            rows = getattr(index, table)
            if not rows:
                continue
            columns = [f.name for f in fields(rows[0])]
            placeholders = ", ".join("?" * len(columns))
            self._db.executemany(
                f"INSERT INTO {table} ({', '.join(columns)})"
                f" VALUES ({placeholders})",
                [astuple(row) for row in rows],
            )


def _subtree(name: str, roots: str) -> str:
    """A CTE naming every node in a subtree whose root ROOTS selects.

    Org tags and properties are inherited: a paragraph under a headline
    tagged ``:urgent:`` is part of something urgent, and a search filtered
    by that tag should find it.  Only the headline carries the row, so the
    filter walks down ``parent_ref`` to cover everything beneath it.
    """
    return (
        f"{name}(doc_id, ref) AS ("
        f"  {roots}"
        "  UNION"
        "  SELECT n.doc_id, n.ref FROM nodes n"
        f"   JOIN {name} ON n.doc_id = {name}.doc_id"
        f"    AND n.parent_ref = {name}.ref)"
    )


def _within(name: str) -> str:
    """Require the matched node to be inside the subtree NAME covers."""
    return (
        f"AND EXISTS (SELECT 1 FROM {name}"
        f" WHERE {name}.doc_id = f.doc_id AND {name}.ref = f.ref)"
    )


def _marks(values: Iterable[Any]) -> str:
    """A comma-separated ``?`` for each value, for an IN clause."""
    return ", ".join("?" * len(list(values)))
