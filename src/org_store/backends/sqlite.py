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

import contextlib
import hashlib
import json
import sqlite3
import struct
import uuid
from collections.abc import Iterable
from dataclasses import astuple, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..embed import Embedder, EmbeddingError
from ..extract import Index, content_hash, index_document
from ..model import OrgDocument
from ..search import SearchQuery, SearchResult, match_expression
from ..store import DocId, DocumentExists, DocumentNotFound, StoreError

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

-- One vector space per model identity.  Vectors from two models cannot
-- be compared, so each identity has its own vec0 table, named for its
-- space_id and as wide as that model's vectors.  Several can coexist, so
-- machines sharing this file through different models each keep theirs.
CREATE TABLE IF NOT EXISTS vector_spaces (
    space_id   INTEGER PRIMARY KEY,
    identity   TEXT NOT NULL UNIQUE,
    dimensions INTEGER NOT NULL
);

-- Which nodes have a vector in which space, and for what text.  Kept
-- apart from the derived tables above because embeddings are not free to
-- recompute: they cost a call to a model that may not even be running,
-- so a write must not depend on one.  The text hash is what lets a later
-- pass embed only what actually changed.
CREATE TABLE IF NOT EXISTS embeddings (
    doc_id    TEXT NOT NULL,
    ref       TEXT NOT NULL,
    space_id  INTEGER NOT NULL REFERENCES vector_spaces(space_id),
    text_hash TEXT NOT NULL,
    vec_id    INTEGER NOT NULL,
    PRIMARY KEY (doc_id, ref, space_id)
);

CREATE INDEX IF NOT EXISTS embeddings_by_doc ON embeddings(doc_id);
CREATE INDEX IF NOT EXISTS embeddings_by_space ON embeddings(space_id);
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

#: The single vector table of the schema before vector spaces, dropped
#: along with its ``embeddings`` rows when an old store is opened.
OLD_VEC_TABLE = "vec_nodes"


def vec_table(space_id: int) -> str:
    """The vector table of one space.  It cannot be created with the
    schema because its width is the model's, not known until one is used."""
    return f"vec_space_{space_id}"

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
        self._vec_loaded = False
        try:
            self._retire_single_space()
            self._db.executescript(SCHEMA)
            self._db.commit()
        except sqlite3.Error as exc:
            self._db.close()
            raise StoreError(f"{database}: {exc}") from exc

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

    # -- embeddings ---------------------------------------------------------

    def embed(
        self, embedder: Embedder, doc_ids: Iterable[DocId] | None = None
    ) -> dict[str, int]:
        """Give every node that needs one an up-to-date vector from EMBEDDER.

        Incremental by text: a node whose text has not changed since it
        was embedded is left alone, because embedding is the expensive
        part and most of a document is unchanged on most writes.  Only
        EMBEDDER's space is written; vectors of other identities are kept,
        except those of nodes that no longer exist.

        Returns what it did, so a caller can say so rather than guess.
        """
        space = self._space(embedder, create=True)
        wanted = self._embeddable(doc_ids)
        removed = self._drop_orphans()
        stale = self._stale(space, wanted)
        tally = {"nodes": len(wanted), "embedded": 0, "removed": removed}
        if not stale:
            return tally

        vectors = embedder.embed_documents([text for *_, text in stale])
        with self._db:
            for (doc_id, ref, digest, _), vector in zip(stale, vectors, strict=True):
                self._store_vector(space, doc_id, ref, digest, vector)
        tally["embedded"] = len(stale)
        return tally

    def semantic_search(
        self, query: SearchQuery, embedder: Embedder
    ) -> list[SearchResult]:
        """Answer a query by nearest neighbour among EMBEDDER's vectors."""
        space = self._space(embedder)
        if space is None:
            return []

        wanted = _pack(embedder.embed_query(query.text or ""))
        # Ask for more than needed: the qualifiers below filter after the
        # nearest-neighbour search, which knows nothing about them.
        depth = max(query.top_k * 8, 64)
        rows = self._db.execute(
            "SELECT rowid AS vec_id,"
            "       vec_distance_cosine(embedding, ?) AS cosine_distance"
            f" FROM {vec_table(space)}"
            " WHERE embedding MATCH ? AND k = ?"
            " ORDER BY distance",
            (wanted, wanted, depth),
        ).fetchall()
        if not rows:
            return []

        # A zero vector has no direction, so no cosine: call it unrelated.
        found = {
            row["vec_id"]: 1.0 if row["cosine_distance"] is None
            else row["cosine_distance"]
            for row in rows
        }
        meta = self._db.execute(
            "SELECT e.doc_id AS doc_id, e.ref AS ref, e.vec_id AS vec_id,"
            "       n.type AS type"
            "  FROM embeddings e"
            "  JOIN nodes n ON n.doc_id = e.doc_id AND n.ref = e.ref"
            f" WHERE e.vec_id IN ({_marks(found)}) AND e.space_id = ?",
            (*found, space),
        ).fetchall()

        results = [
            SearchResult(
                doc_id=row["doc_id"],
                ref=row["ref"],
                type=row["type"],
                # Cosine similarity: 1 for the same direction, 0 for
                # unrelated, and independent of how the model scales its
                # vectors, so it reads the same whichever model is in use.
                score=1.0 - found[row["vec_id"]],
                excerpt=self._excerpt(row["doc_id"], row["ref"]),
            )
            for row in meta
            if self._passes(query, row["doc_id"], row["ref"], row["type"])
        ]
        results.sort(key=lambda result: result.score, reverse=True)
        return results[: query.top_k]

    def embedding_status(self) -> dict[str, Any]:
        """How many nodes there are, and how many vectors each identity has."""
        total = self._db.execute(
            "SELECT count(*) AS n FROM fts_nodes"
        ).fetchone()["n"]
        rows = self._db.execute(
            "SELECT s.identity AS identity, s.dimensions AS dimensions,"
            "       count(e.vec_id) AS vectors"
            "  FROM vector_spaces s"
            "  LEFT JOIN embeddings e ON e.space_id = s.space_id"
            " GROUP BY s.space_id ORDER BY s.identity"
        ).fetchall()
        return {
            "nodes": total,
            "identities": {
                row["identity"]: {
                    "vectors": row["vectors"],
                    "dimensions": row["dimensions"],
                }
                for row in rows
            },
        }

    def embedded_identities(self) -> set[str]:
        """The identities with at least one vector here."""
        return {
            row["identity"]
            for row in self._db.execute(
                "SELECT DISTINCT s.identity AS identity"
                "  FROM vector_spaces s"
                "  JOIN embeddings e ON e.space_id = s.space_id"
            )
        }

    # -- embedding internals ------------------------------------------------

    def _retire_single_space(self) -> None:
        """Drop the vectors of a store from before vector spaces.

        Those were tagged with a model name in a single table, and are not
        worth converting: ``embed`` makes them again.
        """
        columns = {
            row["name"]
            for row in self._db.execute("PRAGMA table_info(embeddings)")
        }
        if "model" not in columns:
            return
        old = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?", (OLD_VEC_TABLE,)
        ).fetchone()
        if old is not None:
            # A vec0 table cannot be dropped without the extension; left
            # behind it is unused and harmless.
            with contextlib.suppress(StoreError, sqlite3.OperationalError):
                self._load_vec()
                self._db.execute(f"DROP TABLE {OLD_VEC_TABLE}")
        self._db.execute("DROP TABLE embeddings")
        self._db.commit()

    def _space(self, embedder: Embedder, create: bool = False) -> int | None:
        """The space_id of EMBEDDER's identity, made on demand if CREATE.

        None when it has no space and none was to be made.  A space keeps
        the width it was made with, so a model claiming the identity with
        another width is refused rather than mixed in.
        """
        row = self._db.execute(
            "SELECT space_id, dimensions FROM vector_spaces WHERE identity = ?",
            (embedder.name,),
        ).fetchone()
        if row is not None:
            if row["dimensions"] != embedder.dimensions:
                raise EmbeddingError(
                    f"vectors for {embedder.name} here have {row['dimensions']}"
                    f" dimensions, not the {embedder.dimensions} configured"
                )
            self._load_vec()
            return row["space_id"]
        if not create:
            return None
        self._load_vec()
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO vector_spaces (identity, dimensions) VALUES (?, ?)",
                (embedder.name, embedder.dimensions),
            )
            space = cursor.lastrowid
            self._db.execute(
                f"CREATE VIRTUAL TABLE {vec_table(space)} USING"
                f" vec0(embedding float[{embedder.dimensions}])"
            )
        return space

    def _load_vec(self) -> None:
        """Load sqlite-vec into this connection, once."""
        if self._vec_loaded:
            return
        try:
            import sqlite_vec
        except ImportError as exc:  # pragma: no cover - optional extra
            raise StoreError(
                "semantic search needs the semantic extra: "
                f"install orgface[semantic] ({exc})"
            ) from exc
        try:
            self._db.enable_load_extension(True)
            sqlite_vec.load(self._db)
        except (AttributeError, sqlite3.OperationalError) as exc:
            raise StoreError(
                f"this Python's sqlite cannot load extensions: {exc}"
            ) from exc
        finally:
            with contextlib.suppress(AttributeError):
                self._db.enable_load_extension(False)
        self._vec_loaded = True

    def _embeddable(
        self, doc_ids: Iterable[DocId] | None
    ) -> list[tuple[str, str, str, str]]:
        """``(doc_id, ref, text_hash, text)`` for every node worth embedding.

        The text is the one the full-text index already holds, so the two
        searches are looking at the same thing.
        """
        sql = "SELECT doc_id, ref, heading_text, body_text FROM fts_nodes"
        params: list[Any] = []
        if doc_ids is not None:
            wanted = list(doc_ids)
            if not wanted:
                return []
            sql += f" WHERE doc_id IN ({', '.join('?' * len(wanted))})"
            params = wanted
        out = []
        for row in self._db.execute(sql, params):
            text = " ".join(
                part for part in (row["heading_text"], row["body_text"]) if part
            ).strip()
            if text:
                out.append(
                    (row["doc_id"], row["ref"], _text_hash(text), text)
                )
        return out

    def _stale(
        self, space: int, wanted: list[tuple[str, str, str, str]]
    ) -> list[tuple[str, str, str, str]]:
        """Those whose vector in SPACE is missing or outdated."""
        current = {
            (row["doc_id"], row["ref"]): row["text_hash"]
            for row in self._db.execute(
                "SELECT doc_id, ref, text_hash FROM embeddings WHERE space_id = ?",
                (space,),
            )
        }
        return [
            entry
            for entry in wanted
            if current.get((entry[0], entry[1])) != entry[2]
        ]

    def _drop_orphans(self) -> int:
        """Forget vectors, in every space, of nodes no longer worth one.

        Judged against the whole store, not just the documents being
        embedded, so embedding a subset leaves the rest alone.
        """
        keep = {(doc_id, ref) for doc_id, ref, _, _ in self._embeddable(None)}
        doomed = [
            row
            for row in self._db.execute(
                "SELECT doc_id, ref, space_id, vec_id FROM embeddings"
            ).fetchall()
            if (row["doc_id"], row["ref"]) not in keep
        ]
        if not doomed:
            return 0
        self._load_vec()
        with self._db:
            for row in doomed:
                self._db.execute(
                    f"DELETE FROM {vec_table(row['space_id'])} WHERE rowid = ?",
                    (row["vec_id"],),
                )
                self._db.execute(
                    "DELETE FROM embeddings"
                    " WHERE doc_id = ? AND ref = ? AND space_id = ?",
                    (row["doc_id"], row["ref"], row["space_id"]),
                )
        return len(doomed)

    def _store_vector(
        self,
        space: int,
        doc_id: str,
        ref: str,
        digest: str,
        vector: list[float],
    ) -> None:
        table = vec_table(space)
        previous = self._db.execute(
            "SELECT vec_id FROM embeddings"
            " WHERE doc_id = ? AND ref = ? AND space_id = ?",
            (doc_id, ref, space),
        ).fetchone()
        if previous is not None:
            self._db.execute(
                f"DELETE FROM {table} WHERE rowid = ?", (previous["vec_id"],)
            )
        cursor = self._db.execute(
            f"INSERT INTO {table}(embedding) VALUES (?)", (_pack(vector),)
        )
        self._db.execute(
            "INSERT INTO embeddings (doc_id, ref, space_id, text_hash, vec_id)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(doc_id, ref, space_id) DO UPDATE SET"
            "  text_hash = excluded.text_hash,"
            "  vec_id = excluded.vec_id",
            (doc_id, ref, space, digest, cursor.lastrowid),
        )

    def _passes(self, query: SearchQuery, doc_id: str, ref: str, kind: str) -> bool:
        """Apply the qualifiers a nearest-neighbour search cannot."""
        if query.doc_ids and doc_id not in query.doc_ids:
            return False
        if query.node_types and kind not in query.node_types:
            return False
        if query.tags and not self._within("tagged", query.tags, doc_id, ref):
            return False
        for key, value in (query.property_filters or {}).items():
            if not self._within("prop", [key, value], doc_id, ref):
                return False
        return True

    def _within(self, kind: str, values: list[str], doc_id: str, ref: str) -> bool:
        """Whether a node is inside a subtree the filter covers."""
        if kind == "tagged":
            roots = (
                "SELECT doc_id, ref FROM tags"
                f" WHERE tag IN ({_marks(values)})"
            )
            params: list[Any] = list(values)
        else:
            roots = "SELECT doc_id, ref FROM properties WHERE key = ? AND value = ?"
            params = list(values)
        sql = (
            "WITH RECURSIVE " + _subtree("covered", roots) + " SELECT 1 FROM covered"
            " WHERE covered.doc_id = ? AND covered.ref = ? LIMIT 1"
        )
        return (
            self._db.execute(sql, (*params, doc_id, ref)).fetchone() is not None
        )

    def _excerpt(self, doc_id: str, ref: str) -> str:
        row = self._db.execute(
            "SELECT heading_text, body_text FROM fts_nodes"
            " WHERE doc_id = ? AND ref = ?",
            (doc_id, ref),
        ).fetchone()
        if row is None:
            return ""
        text = " ".join(
            part for part in (row["heading_text"], row["body_text"]) if part
        )
        return " ".join(text.split())[:200]

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
        payload = json.dumps(doc.dump(), sort_keys=True)
        # Over the rendered org text, so that this store's hashes are
        # comparable with a file-backed store's.  See extract.content_hash.
        digest = content_hash(doc)
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


def _pack(vector: list[float]) -> bytes:
    """A vector in the little-endian float32 form sqlite-vec stores."""
    return struct.pack(f"<{len(vector)}f", *vector)


def _text_hash(text: str) -> str:
    """What a node's embedded text was, so an unchanged one is left alone."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
