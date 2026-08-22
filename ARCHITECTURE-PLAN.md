# org-cli-helper: org-store architecture plan

## 0. What this document is

A design and phased implementation plan for extending `org-cli-helper` from a
capture/dailies CLI into an abstracted, multi-backend store for org content,
with CRUDS operations exposed via Python API, CLI, and MCP.

This was produced from a design conversation covering: JSON schemas for
document ASTs (MyST, pandoc, ADF, Notion), math/LaTeX handling, `ox-json` vs
`uniorg` vs `org-parser` vs `orgize` for org→JSON, and a live inspection of
both `ox-json`'s documented output shape and `org-parser` 0.28.0's actual
Python object model (installed and exercised in a sandbox — not just read
from docs). Findings from that inspection are baked into the design below,
especially §4.3 and Appendix A.

**Audience**: a fresh Claude Code session working inside this repository. It
should read this whole document before writing code, then work phase by
phase (§8), checking in at each phase boundary rather than implementing
everything in one pass.

---

## 1. Ground truth: what already exists in this repo

Before designing anything new, build on what's here. Do not reintroduce a
second Emacs RPC mechanism, a second CLI framework, or a second config
system.

- **CLI**: `click`-based, `src/org_cli_helper/cli.py`. A `click.Group`
  called `main`, a shared `Context` dataclass carrying `Config`, subcommands
  registered in `_register()` from `src/org_cli_helper/commands/`. New
  commands follow this exact pattern — one module per command group under
  `commands/`, added to `_register()`.
- **Layering rule** (from `CLAUDE.md`, non-negotiable): CLI and UI modules
  contain **no data-processing logic**. Logic lives in plain modules; CLI
  modules only parse arguments, call into logic modules, and render via
  `ui.py`. This maps directly onto the ports-and-adapters shape this plan
  proposes (§3) — the CLI is one adapter among several (CLI, MCP, FastAPI),
  all thin, all calling the same service layer.
- **Emacs daemon management**: `src/org_cli_helper/emacs.py` already
  implements a robust ssh-ControlMaster-style daemon: `Emacs.start()`,
  `.stop()`, `.eval(form)`, `.eval_json(form)`, autostart-on-demand, idle
  lifetime, a `bootstrap.el` loaded lazily. **Use this, do not adopt
  `pyorg`/`python-emacs`.** Those were evaluated during design and rejected:
  `pyorg` is a thin RPC client requiring a live Emacs process for every
  read, its write-back-to-org path is unconfirmed, and the upstream project
  shows signs of being dormant. This repo's own `Emacs` class is cleaner,
  tested (`tests/test_emacs_live.py`), and already solves the hard part —
  getting structured data out of Emacs without reverse-engineering `prin1`
  output — via `elisp.decode_payload`.
- **Elisp/JSON bridge**: `src/org_cli_helper/elisp.py`. `emacsclient --eval`
  prints Elisp's `prin1` representation, which is painful to parse
  faithfully; this repo sidesteps that by having the daemon-side Elisp
  base64-encode a UTF-8 JSON payload and print *that* as a quoted string,
  which `elisp.decode_payload` unwraps. **Reuse this verbatim for `ox-json`
  export** (§4.2) rather than inventing a second Elisp-to-Python channel.
- **Existing `templates.py`**: this is org-capture template selection/fill
  logic for the dailies/note workflow — **unrelated** to the new Jinja2
  rendering templates this plan introduces (§4.4). Name the new module
  something else (`render/` or `orgrender.py`) to avoid confusion; do not
  extend `templates.py` for this purpose.
- **Config**: XDG-convention, layered env → file → CLI flags
  (`src/org_cli_helper/config.py`). New settings (store backend selection,
  sqlite path, org directory root, embedding model, etc.) belong in this
  existing `Config` dataclass and its `load()` function, following the same
  layering.
- **Testing conventions**: `tests/`, run via `uv run pytest`, a registered
  `emacs` marker for tests needing a real Emacs (`pytest -m "not emacs"` to
  skip). Tests are focused, independent, and avoid mocking internals — favor
  real fixtures (real org text, a real temp sqlite file, a real temp Emacs
  daemon under the `emacs` marker) over mocks.
- **Dependencies today**: just `click` (+`pytest` dev). Everything in this
  plan (`pydantic`, `org-parser`, `jinja2`, `fastapi`, an MCP SDK, a vector
  index) is a new addition — add via `uv add`, keep optional/extra groups
  where a backend is genuinely optional (see §7).

---

## 2. Goals and non-goals

**Goals**

1. A single canonical, versioned, schema-validated JSON representation of
   org content (`ox-json`'s shape), with a pydantic model for it.
2. Multiple interchangeable store backends behind one abstract interface:
   sqlite, a plain org-file directory (via `org-parser` + Jinja2), and an
   Emacs-buffer-backed directory (via the existing `Emacs` class + `ox-json`).
3. A CRUDS Python service layer over the store abstraction, addressable at
   both whole-document and sub-document (node) granularity.
4. Symbolic (full-text/grep) and semantic (embedding) search, scoped by
   node kind (heading vs. body vs. property, etc.).
5. The same service layer exposed three ways: extended CLI, MCP server, and
   (as a schema/typing aid, not necessarily a deployed service) FastAPI.
6. A sync mechanism to move content between store backends.

**Non-goals for v1** (explicitly deferred — see §9 for why)

- Perfect round-trip byte-fidelity for every obscure org construct on the
  `org-parser` ingestion path. Two concrete, *confirmed* gaps exist (LaTeX
  math/environments, footnote-definitions — see §4.3) and are handled by a
  documented promotion step, not by extending the parser first.
- Real-time bidirectional sync with automatic conflict resolution. v1 sync
  is one-directional and explicit; conflict handling is a v2 problem.
- A fully general relational model of every org construct in sqlite. The
  sqlite design (§5.1) is JSON-as-source-of-truth plus a *small, deliberate*
  set of derived indexes, not an ORM mapping of the whole schema.
- Writing back to org files through Emacs when *nothing* about the change
  requires Emacs (i.e., prefer the org-parser+Jinja2 write path by default;
  reserve the Emacs-backed store for when Emacs-specific fidelity is the
  point — see §4.6).

---

## 3. High-level architecture

```
                         ┌───────────────────────────────┐
                         │   canonical pydantic model    │
                         │   (org_store.model)           │
                         │   + generated JSON Schema     │
                         └──────────────┬────────────────┘
                                        │
                 ┌──────────────────────┼──────────────────────┐
                 │                      │                      │
        ┌────────▼─────────┐   ┌────────▼────────┐   ┌─────────▼───────────┐
        │  StoreBackend    │   │  StoreBackend   │   │   StoreBackend      │
        │  sqlite          │   │  org-parser +   │   │   Emacs-backed      │
        │  (org_store.     │   │  Jinja2 files   │   │   (existing Emacs   │
        │   backends.sqlite)│  │  (org_store.    │   │    class + ox-json  │
        │                  │   │   backends.orgfs)│  │    org_store.       │
        │                  │   │                 │   │    backends.emacsfs)│
        └────────┬─────────┘   └────────┬────────┘   └─────────┬───────────┘
                 │                      │                      │
                 └──────────────────────┼──────────────────────┘
                                        │  all implement the same
                                        │  Protocol (org_store.store.StoreBackend)
                             ┌──────────▼─────────────┐
                             │   CRUDS service        │
                             │   (org_store.service)  │
                             │   + Search subsystem   │
                             │   (org_store.search)   │
                             └────────────┬───────────┘
                     ┌────────────────────┼────────────────────┐
             ┌───────▼────────┐  ┌────────▼─────────┐  ┌───────▼─────────┐
             │  CLI adapter   │  │  FastAPI adapter │  │  MCP adapter    │
             │  (extends the  │  │  (schema +       │  │  (org_cli_helper│
             │  existing      │  │  optional HTTP)  │  │  .mcp)          │
             │  click app)    │  │                  │  │                 │
             └────────────────┘  └──────────────────┘  └─────────────────┘

                    ┌───────────────────────────────┐
                    │   Sync engine (org_store.sync)│
                    │   reads/writes stores only    │
                    │   through the CRUDS service,  │
                    │   never backend internals     │
                    └───────────────────────────────┘
```

Key architectural rule, worth stating explicitly since it will be tempting
to violate under deadline pressure: **the service layer is the only thing
that knows about CRUDS semantics; adapters (CLI/FastAPI/MCP) are thin and
call the service layer; store backends are the only things that know about
sqlite/org-parser/Jinja2/Emacs.** The sync engine talks to stores exclusively
through the CRUDS service's public API — never by reaching into one
backend's internals to shortcut into another's.

---

## 4. The canonical schema

### 4.1 Pydantic model of `ox-json`'s shape

`ox-json` exports every org-element node as:

```json
{
  "$$data_type": "org-node",
  "type": "headline",
  "ref": "org3f9a1c2",
  "properties": { "...": "raw org-element properties, keys without the leading colon" },
  "contents": [ /* child nodes, or plain strings for text */ ]
}
```

with a document wrapper carrying file-level keywords (`title`, `filetags`,
`author`, `creator`, `date`, `description`, `email`, `language`) in its own
`properties`.

Model this in `org_store/model.py` as:

- `OrgDocument` — the document wrapper: `properties: DocumentProperties`,
  `contents: list[OrgNode]`.
- `OrgNode` — `data_type: Literal["org-node"]` (alias `$$data_type`),
  `type: str`, `ref: str`, `properties: dict[str, Any]`,
  `contents: list[OrgNode | str]`.
- Consider a **discriminated union** on `type` for the node types you
  actively operate on (headline, section, paragraph, src-block,
  quote-block, example-block, link, timestamp, property-drawer,
  latex-fragment, latex-environment — see §4.3 on the last two) with typed
  `properties` submodels for each, falling back to `OrgNode` with a raw
  `dict` for everything else. This gets you real type checking and
  autocomplete for the ~15 node types you'll touch constantly, without
  having to enumerate all ~60 of org-element's types before you can ship
  anything. Grow the discriminated set incrementally.
- Use pydantic v2. `Model.model_json_schema()` gives you the JSON Schema
  directly — commit the generated schema to the repo (e.g.
  `schema/org-node.schema.json`) as a build artifact, regenerated by a
  script/test rather than hand-maintained, and version it (see §4.7).

### 4.2 `ox-json` read path (Emacs is the source of truth)

Extend, don't replace, the existing daemon machinery:

1. Add `ox-json` as an Elisp dependency the bootstrap can `require`. Add an
   Elisp function alongside the existing ones in `bootstrap.el` (pattern:
   look at how `org-cli-helper-info`/`org-cli-helper-commit` are defined),
   e.g. `org-cli-helper-export-json (path)`, that visits or reuses a buffer
   for `path`, runs `ox-json`'s export, base64-encodes the UTF-8 JSON
   result the same way the existing `commit`/`note` handlers do, and
   returns it.
2. Add `Emacs.export_json(self, path: Path) -> dict[str, Any]` to
   `emacs.py`, calling `self.eval_json(elisp.call("org-cli-helper-export-json", str(path)))`.
   This is the entire integration surface with `ox-json` — no new Elisp
   bridge needed, no `pyorg`, no `python-emacs`.
3. Validate the returned dict against the pydantic model
   (`OrgDocument.model_validate(...)`) immediately at this boundary. This is
   your live conformance check against real Emacs/org-element behavior,
   including whatever custom link types, macros, or in-buffer settings are
   in the person's actual config — something no independent parser can
   promise.

### 4.3 `org-parser` read path (independent, no Emacs required)

`org-parser` (tree-sitter-org based) does **not** emit JSON — it exposes
frozen dataclasses, each privately holding a non-serializable
`tree_sitter.Node`. Building the walker that turns its object tree into the
canonical `OrgDocument`/`OrgNode` model is real work, but the type-dispatch
table is now known precisely (confirmed by installing `org-parser==0.28.0`
and inspecting it directly, not from documentation):

- Document-level: `doc.title`, `doc.keywords` (list of `Keyword(key, value: RichText)`
  — covers `TITLE`/`AUTHOR`/`FILETAGS`/`DATE`/`CATEGORY`/`DESCRIPTION`/`TODO`
  generically), `doc.properties` (the file-level `:PROPERTIES:` drawer only
  — **do not confuse this with `doc.keywords`**, they are genuinely
  different things). `FILETAGS` comes back as a raw string
  (`":physics:dune:"`) — split on `:` yourself; `ox-json` gives you a
  pre-split array, so the mapping layer must do this normalization.
- Blocks: `CenterBlock`, `QuoteBlock`, `SpecialBlock`, `DynamicBlock`,
  `VerseBlock`, `CommentBlock`, `ExampleBlock`, `ExportBlock`, `SourceBlock`
  (fields: `language`, `switches`, `body`), `FixedWidthBlock`.
- Drawers: `Drawer`, `Logbook`, `Properties` (a `MutableMapping`).
- Keywords: `Keyword`, `AffiliatedKeyword`, `CaptionKeyword`,
  `TblnameKeyword`, `ResultsKeyword`, `PlotKeyword`.
- Lists: `List`, `ListItem`, `Repeat`.
- Tables: `Table` (→ `rows: list[TableRow | TableRuleRow]`), `TableRow`
  (→ `cells: list[TableCell]`), `TableCell`, `TableEl` (the alternate
  `#+begin: table.el` form).
- Structural: `BlankLine`, `Comment`, `HorizontalRule`, `Indent`.
- Inline objects (`text/_inline.py`): `PlainText`, `LineBreak`,
  `InlineEntity`, `CompletionCounter` (statistics cookie), `Bold`, `Italic`,
  `Underline`, `StrikeThrough`, `Subscript`, `Superscript`, `Verbatim`,
  `Code`, `ExportSnippet`, `FootnoteReference` (has `label`, `definition`),
  `Citation`, `InlineSourceBlock`, `Macro`, `InlineBabelCall`, `PlainLink`,
  `AngleLink`, `RegularLink` (`path`, `description: list[InlineObject]`),
  `Target`, `RadioTarget`.
  **Confirmed good news**: emphasis genuinely nests
  (`Bold(body=[..., Italic(body=[...]), ...])`, not a flat marks list) —
  this maps cleanly onto `OrgNode.contents` recursion, no special-casing
  needed.
- Time: `Timestamp` (`is_active`, `start_year`/`start_month`/`start_day`/
  `start_dayname`, plus end-* fields), `Repeater`, `Clock`.
- `BabelCall`.

**Two confirmed gaps, both empirically verified, not inferred:**

1. **No LaTeX fragment/environment node type at all.** `$E=mc^2$` and a
   full `\begin{equation}...\end{equation}` block both parse as inert
   `PlainText` inside a `Paragraph` — completely indistinguishable from
   ordinary prose. This directly affects the math-handling design discussed
   separately: verbatim capture is still the right target representation
   (a `latex-fragment`/`latex-environment` node with a raw `value` string
   and a `display: inline|block` field — mirroring what org-element itself
   does), but `org-parser` will not hand you that distinction for free.
   **Mitigation**: in the `org-parser` → canonical-JSON walker, post-process
   `PlainText` runs with a regex pass detecting `$...$`, `\(...\)`,
   `\[...\]`, and `\begin{env}...\end{env}` spans, and promote matches to
   your own `latex-fragment`/`latex-environment` nodes *before* they reach
   storage. Do this at the walker boundary, not by waiting on upstream
   grammar support — keeps you independent of that project's timeline.
   Consider filing the gap upstream with the `org-parser`/`tree-sitter-org`
   maintainer once this is working reliably, since the codebase looks
   actively maintained and well-structured.
2. **No block-level footnote-definition type.** `FootnoteReference` tracks
   a `label` and a `definition` field, but nothing populates `definition` —
   `[fn:1] some text` parses as a plain `Paragraph`, with no structural
   link back to the reference. **Mitigation**: similarly, detect
   paragraphs whose rendered text matches `^\[fn:([^\]]+)\]\s` at the
   walker boundary and promote them to a `footnote-definition` node,
   linking by label to any `FootnoteReference` nodes sharing it.

Both mitigations happen in exactly one place — the `org-parser` walker
(`org_store/adapters/orgparse_walk.py` or similar) — so `ox-json`-sourced
documents (§4.2) are unaffected; they get the real distinction from
org-element directly.

### 4.4 Write path: Jinja2 rendering (canonical model → org text)

One Jinja2 template per canonical node `type` (a template directory mapping
`type` → template file, dispatched by a small Python renderer, not one
giant template). Recursive: a `headline` template renders its own stars/
title/tags line then includes the rendered form of each child in
`contents`. This is the render path for the org-file store (§4.5) and,
where useful, a fallback write path for the Emacs-backed store (§4.6).

**Important distinction for partial updates** (this repo's stated
requirement — "operations on portions of a file"): full Jinja2 rendering
of the whole document is for `create` (new file) and whole-document
`replace`. For updating a single node inside an existing file, **do not**
re-render the whole document from the canonical model and overwrite the
file — that risks silently reformatting or losing unrelated hand-edited
content the canonical model doesn't fully capture yet (see the two gaps in
§4.3). Instead:

1. Re-parse the current on-disk file with `org-parser` (which preserves
   exact source spans/whitespace).
2. Locate the target node by its ref/path (§5.3).
3. Render *only that node's subtree* to org text via Jinja2.
4. Splice the new text into the original source at that node's span,
   leaving everything else byte-identical.
5. Write the result back.

`org-parser`'s preserved positions are useful *here*, as an internal
implementation detail of this splice operation — this does not contradict
dropping raw positions from the canonical stored schema (§5.3); positions
are ephemeral, computed fresh from the live file at edit time, never
persisted as part of the canonical JSON.

### 4.5 Node identity / refs

`ox-json` supports deterministic, structural-path-based refs
(`:json-deterministic-refs`). **Decision needed before implementing CRUD
addressing** (flag this explicitly to the person before proceeding past
Phase 1): if refs must be stable and identical regardless of which engine
(`ox-json`/Emacs, or `org-parser`) produced them for the *same* document,
the ref-computation algorithm must be reimplemented identically in the
Python walker — do not assume each engine's own internal ref scheme will
agree with the other's. Recommended approach: define the ref algorithm
once, in Python, as a pure function of structural path (e.g., a sequence of
child indices from the document root, hashed), and use it in the
`org-parser` walker; for the `ox-json` path, either configure Elisp to use
the equivalent deterministic scheme, or discard Emacs's own `ref` and
recompute the same Python-side ref after validation, so both ingestion
paths always agree.

### 4.6 Emacs-backed store, read vs. write

Read path: `Emacs.export_json()` (§4.2) — already gives you canonical-shaped
JSON directly, no walker needed, full org-element fidelity including the
person's own Emacs customization.

Write path is the harder half, and deserves explicit design attention
rather than being assumed trivial:

- **Whole-file replace**: safe to do via the Jinja2 renderer (§4.4) writing
  the file directly — but only if no Emacs buffer for that file is open, or
  if you explicitly tell Emacs to revert it afterward. Silently overwriting
  a file out from under a live, possibly-modified Emacs buffer is a real
  data-loss risk for a system meant to have Emacs as one of several
  concurrent clients.
- **Partial update**: prefer having Emacs perform the edit *in its own
  buffer* — locate the node via `org-element`'s tree (using the same
  structural-path/ref scheme, §4.5), do a targeted
  `delete-region`/`insert`, then `save-buffer` — rather than Python editing
  the file on disk. This keeps Emacs's own undo history and any
  buffer-local state (folding, markers) coherent. This needs a new
  bootstrap.el entry point (e.g. `org-cli-helper-replace-node`) — budget
  real design time for this in Phase 5, it is not a small addition.
- Given the effort/risk here, and that `org-parser`+Jinja2 (§4.4/4.5) is
  independently sufficient for most write traffic, **v1 can treat the
  Emacs-backed store as read-plus-whole-file-write only**, and defer the
  Emacs-side partial-update entry point to a later phase. This also gives
  you a genuinely useful role for it in the meantime: a **conformance
  oracle** — round-trip the same fixtures through both `org-parser` and
  real Emacs/`ox-json` in tests, diff the canonical JSON, and let
  divergences catch walker bugs or grammar gaps early.

### 4.7 Schema versioning

Put a `schema_version` field on `OrgDocument` from day one. Regenerate and
commit the JSON Schema as part of CI (a test that fails if the checked-in
schema file drifts from `model_json_schema()`'s current output), so schema
changes are visible in diffs and reviewable, not silent.

---

## 5. Store backends

All backends implement one `Protocol` in `org_store/store.py`:

```python
class StoreBackend(Protocol):
    def create_document(self, doc: OrgDocument, *, path_hint: str | None = None) -> DocId: ...
    def read_document(self, doc_id: DocId) -> OrgDocument: ...
    def replace_document(self, doc_id: DocId, doc: OrgDocument) -> None: ...
    def delete_document(self, doc_id: DocId) -> None: ...
    def list_documents(self) -> Iterable[DocId]: ...

    def read_node(self, doc_id: DocId, ref: str) -> OrgNode: ...
    def update_node(self, doc_id: DocId, ref: str, node: OrgNode) -> None: ...
    def delete_node(self, doc_id: DocId, ref: str) -> None: ...

    def content_hash(self, doc_id: DocId) -> str: ...  # for sync (§6)
```

Search (§6a) is deliberately **not** on this Protocol — not every backend
can support both symbolic and semantic search equally well or efficiently;
it lives in the service layer, backed by whichever indexes a given backend
(chiefly sqlite) chooses to maintain. A backend that can't search natively
just gets indexed into the sqlite backend's search tables as a side effect,
or is excluded from semantic search until it is.

### 5.1 sqlite backend — the storage-form question, resolved

Recommendation: **hybrid**. Canonical JSON as source of truth, plus a
small, deliberately-scoped set of derived/indexed tables, rebuilt from that
JSON — not an attempt to relationally model org-element's full vocabulary.

```
documents(doc_id PK, path, schema_version, content_json TEXT, content_hash, updated_at)
nodes(ref PK, doc_id FK, type, parent_ref, path_json, heading_level, todo_keyword, priority)
tags(ref FK, tag)
properties(ref FK, key, value)
links(source_ref FK, target, link_type)          -- "find links" queries
timestamps(ref FK, kind, start, end)              -- SCHEDULED/DEADLINE/active
fts_nodes USING fts5(ref UNINDEXED, heading_text, body_text, content='')
```

- `documents.content_json` is authoritative. Every side table is derived
  from it by a single `reindex(doc)` function called on every write —
  never written to independently. If the mapping logic changes, you can
  always `DROP` and rebuild every side table from `content_json` with no
  data loss, which is the whole point of treating them as indexes rather
  than a second source of truth.
- `nodes`/`tags`/`properties`/`links`/`timestamps` exist because they
  answer the structured queries explicitly anticipated: tag search,
  property-predicate search, "find all links to X" (needed for a backlink
  graph — start this table even though nothing consumes it yet in v1, it's
  cheap to populate and expensive to retrofit), and scheduling/agenda-style
  queries. Don't add a table for a query pattern you don't have yet — use
  SQLite's JSON1 functions (`json_extract`, `json_each`) directly against
  `content_json` for anything ad hoc; promote to a dedicated table only
  once a query pattern is common enough to want an index.
- `fts_nodes` (FTS5) is the symbolic full-text search backend (§6a),
  populated at reindex time with heading text and flattened body text kept
  in separate columns specifically so queries can be scoped to "heading
  only" vs. "body only" vs. both, per the qualifier requirement.
- Store the ref, not a synthetic autoincrement id, as the primary key
  where sensible — refs are already meant to be stable/deterministic
  (§4.5), so this doubles as your cross-store join key when syncing.

### 5.2 org-file directory backend

Read: `org-parser` → walker (§4.3) → canonical model. Write: Jinja2 render,
whole-document or spliced-partial as described in §4.4. `content_hash()` is
a hash of the file bytes (cheap, and meaningful here since files *are* the
storage).

### 5.3 Emacs-backed directory backend

As designed in §4.6: read via `Emacs.export_json`, write via whole-file
Jinja2 (v1) with an Emacs-side partial-update path deferred. `content_hash`
can reuse the same file-bytes hash as §5.2 when reading from disk, or
request a hash computed inside Emacs when you want to be sure you're
hashing the buffer's current (possibly unsaved) state rather than the file.
Decide which of these `content_hash` means and document it — sync (§6)
depends on it meaning the same thing consistently.

---

## 6. Sync engine

v1 scope: **one-directional, explicit, no automatic conflict resolution.**
`sync(source: StoreBackend, dest: StoreBackend, doc_ids: Iterable[DocId] | None = None)`
walks `source.list_documents()` (or a given subset), compares
`content_hash()` against a previously-recorded hash for that doc_id in
`dest` (stored wherever the sync engine keeps its own bookkeeping — a small
sqlite table of its own, or reuse the sqlite backend's `documents` table if
one side *is* the sqlite backend), and for anything changed, reads the full
`OrgDocument` from `source` and calls `dest.replace_document(...)` — always
through the CRUDS service, never backend-internal APIs.

Explicitly out of scope for v1: detecting when *both* sides changed since
the last sync (a real three-way-merge problem) — for now, last-sync-wins in
whichever direction the person invokes, and the CLI command should say so
plainly rather than implying safety it doesn't have. Revisit once the
single-directional path is solid and in daily use.

---

## 6a. Search subsystem

`org_store/search.py`, a `SearchQuery` pydantic model and a `search()`
function in the service layer (not the store Protocol — see §5).

```python
class SearchQuery(BaseModel):
    text: str | None = None
    semantic: bool = False
    scope: Literal["heading", "body", "all"] = "all"
    node_types: list[str] | None = None
    tags: list[str] | None = None
    property_filters: dict[str, str] | None = None
    top_k: int = 20
```

- **Symbolic**: SQLite FTS5 against `fts_nodes` (§5.1), scoped by the
  `heading_text`/`body_text` column split for the heading/body qualifier,
  further filtered by joining `tags`/`properties`/`nodes.type` for the
  other qualifiers. This is the default, always-available search path —
  works for anything indexed into the sqlite backend regardless of which
  backend is the primary store for that content (see §5, "excluded until
  indexed").
- **Semantic**: embed node text (probably per-paragraph/per-heading rather
  than per-document) into a vector index. Given prior work already done
  outside this repo on local embedding generation for org-roam content
  (nomic-embed-text via llama.cpp), that's a reasonable default model
  choice to start from rather than introducing a new dependency
  cold — but keep the embedding step behind a small `Embedder` protocol so
  the model/runtime is swappable. For the vector index itself, `sqlite-vec`
  keeps everything in the same sqlite file as the rest of the store
  (simplest ops story); evaluate it first before reaching for an external
  vector database. Store embeddings keyed by `ref`, recomputed at reindex
  time alongside the FTS5/side-table updates, only for nodes whose text
  actually changed (hash-gate this — re-embedding on every write regardless
  of change is wasteful).
- Both search modes return the same `SearchResult` shape (`ref`, `doc_id`,
  `score`, a short excerpt) so CLI/MCP/FastAPI adapters don't need to know
  which mode produced a given result.

---

## 7. Adapters

### 7.1 CRUDS service layer

`org_store/service.py`. Plain functions or a thin class wrapping a chosen
`StoreBackend` (which backend is active is a `Config` setting, following
the existing layered-config convention). This is the *only* layer with
CRUDS business logic — validation, ref resolution, calling `reindex()`
after writes, calling into `search.py`. Every adapter below is a thin
wrapper around this.

### 7.2 FastAPI

Use FastAPI primarily as a **schema and typing tool**, not necessarily as a
deployed HTTP service on day one: define the CRUDS operations as FastAPI
path-operation functions with the pydantic models from §4.1/§6a as
request/response types, purely so (a) OpenAPI/JSON-Schema generation comes
for free and stays in sync with the actual API shape, and (b) it's a
natural source to mechanically derive MCP tool schemas from (§7.4) — check
current tooling for FastAPI→MCP schema translation before hand-writing that
mapping, since this space moves quickly. Keep all real logic in
`org_store/service.py`; FastAPI route handlers should be one line calling
into it, same as the CLI commands.

### 7.3 CLI

Extend the existing `click` app following its own conventions exactly
(§1): new command modules under `commands/` (e.g. `commands/store.py`,
`commands/node.py`, `commands/search.py`), each calling `org_store.service`
functions only, registered in `cli.py`'s `_register()`. Respect the
existing rule that every command shows help with no args, accepts `-h`,
and does one thing then exits.

### 7.4 MCP

New module, e.g. `org_cli_helper/mcp.py` or a small `org_store.mcp`
subpackage, exposing CRUDS + search as MCP tools, calling
`org_store.service` directly (not through the FastAPI HTTP layer — no
reason to round-trip through HTTP for an in-process MCP server). Worth
looking at `org-mcp-server` (Rust, `orgize`+`rmcp`) as a prior-art reference
for what a reasonable initial tool surface looks like (read/list/search,
TODO-state and property edits, fuzzy search) even though this
implementation is independent — no need to match it exactly, but no reason
to reinvent tool-naming conventions from scratch either.

---

## 8. Phased delivery plan

Each phase should be independently mergeable and leave the repo in a
working, tested state — don't let phases bleed into each other.

**Phase 0 — scaffolding**
New `org_store` package (top-level module or `src/org_cli_helper/org_store/`
— decide based on whether this is conceptually a library other tools might
import independent of the CLI; a top-level `src/org_store/` alongside
`src/org_cli_helper/` is probably cleaner given the FastAPI/MCP surface).
Add `pydantic`, `org-parser`, `jinja2` as dependencies via `uv add`. No
behavior yet — just structure and CI wiring for schema-drift checks (§4.7).

**Phase 1 — canonical model**
`org_store/model.py` (§4.1). Golden-fixture tests: hand-written JSON
matching real `ox-json` output shape (captured once from a real Emacs, or
transcribed carefully from `ox-json`'s README examples if no Emacs is
available in this environment — flag which one it ended up being) validated
against the pydantic model. Generate and commit the JSON Schema; add the
drift-check test.

**Phase 2 — sqlite backend + CRUDS core + minimal CLI**
Implement `StoreBackend` for sqlite (§5.1) against the Phase 1 model
directly (skip org-file/Emacs backends for now — get CRUD semantics right
against the simplest backend first). Implement `org_store/service.py`'s
create/read/replace/delete. Add one CLI command group
(`org-cli-helper store ...`) to dogfood it early, per this repo's existing
practice of testing through real usage rather than mocks.

**Phase 3 — symbolic search**
FTS5 tables + `SearchQuery`/`search()` for symbolic mode only (§6a). Wire
into CLI. This is small on top of Phase 2 and gives immediate value before
tackling the harder backends.

**Phase 4 — org-parser + Jinja2 file backend**
The walker (§4.3, including the two documented mitigations), the Jinja2
renderer (§4.4, both whole-document and node-splice paths), and
`StoreBackend` for the org-file directory. Golden round-trip tests: parse a
representative fixture corpus, render, re-parse, assert semantic
equivalence (not necessarily byte-identical for edge cases you haven't
handled yet — but flag any file where it isn't, don't silently accept
lossy round trips).

**Phase 5 — Emacs-backed backend**
`Emacs.export_json` (§4.2) plus the bootstrap.el addition. Read path only,
using the whole-file Jinja2 write path from Phase 4 for writes (§4.6).
Add the conformance-oracle test suite: same fixtures through both Phase 4's
walker and this backend, diff canonical JSON, log/triage divergences.
Emacs-side partial-update (targeted buffer edit) is a stretch goal for this
phase, not a blocker — land read+whole-file-write first.

**Phase 6 — sync engine v1**
One-directional sync (§6) between any two `StoreBackend`s, CLI command,
tests covering the sqlite↔org-file direction at minimum.

**Phase 7 — semantic search**
`Embedder` protocol, `sqlite-vec` integration, hash-gated re-embedding on
reindex, wire `semantic=True` into `SearchQuery`/`search()`.

**Phase 8 — FastAPI schema layer**
Route definitions per §7.2, OpenAPI generation, decide (and document the
decision) whether an actual HTTP server is deployed in this phase or the
FastAPI app object is used purely as a schema source for Phase 9.

**Phase 9 — MCP service**
Tool surface per §7.4, wired to `org_store.service` directly.

At each phase boundary, stop and confirm scope with the person before
continuing — several sections above (§4.5 ref determinism, §5.1 side-table
scope, §6a embedding model choice) are marked as decisions, not settled
facts, and should be revisited with current information rather than
locked in by an early phase.

---

## 9. Explicit open questions / decisions needed

Collected here so they're not lost in the prose above.

1. **Ref determinism across engines** (§4.5): does the Python-side ref
   algorithm need to exactly match `ox-json`'s own deterministic-ref
   scheme, or is it acceptable for the canonical layer to always compute
   its own refs post-validation regardless of source engine? The latter is
   simpler and is the default recommendation above, but confirm before
   Phase 2 since it affects the sqlite primary key design.
2. **sqlite side-table scope** (§5.1): the five tables listed
   (`nodes`/`tags`/`properties`/`links`/`timestamps`) are a starting
   recommendation, not a final list. Revisit once real query patterns from
   CLI/MCP usage exist.
3. **Emacs partial-write safety** (§4.6): what should happen when a
   partial-update request targets a file with a live, modified Emacs
   buffer? Silently overwrite, refuse, or force a `revert-buffer`/merge?
   This needs a real decision before Phase 5's stretch goal, not just
   before it's implemented as an afterthought.
4. **Embedding model/runtime** (§6a): confirm whether to reuse the existing
   nomic-embed-text/llama.cpp setup from prior local-embedding work, or
   choose something else for this project specifically — they don't have
   to be the same choice.
5. **`org_store` package location** (Phase 0): top-level package vs. nested
   under `org_cli_helper`. Affects how cleanly it can be reused/imported
   outside the CLI tool later (e.g., if the FastAPI/MCP surface ever ships
   as its own installable thing).
6. **LaTeX/footnote-definition upstream fix vs. permanent local
   workaround** (§4.3): the promotion-step mitigation is designed to be
   permanent-safe regardless, but worth deciding whether to invest in an
   upstream `tree-sitter-org` contribution at some point, given the
   project's apparent health.

---

## Appendix A — org-parser → canonical node-type mapping (starting table)

Fill in `properties` mappings as the walker is implemented; this is the
class-to-canonical-`type`-string skeleton, derived from direct inspection
of `org-parser` 0.28.0's source (`element/__init__.py`, `text/_inline.py`,
`document/_heading.py`, `time/_timestamp.py`, `_nodes.py`).

| org-parser class | canonical `type` | notes |
|---|---|---|
| `Heading` | `headline` | tags list already inherits filetags — decide whether canonical schema wants that pre-merged or wants file-level/local tags kept separate to match `ox-json`'s `tags`/`tags-all` split |
| `Paragraph` | `paragraph` | run the LaTeX/footnote-definition promotion pass (§4.3) here before finalizing type |
| `SourceBlock` | `src-block` | `language`→`:language`, `switches`→ parse into org-element's header-args shape, `body`→`:value` |
| `QuoteBlock`/`CenterBlock`/`VerseBlock`/`SpecialBlock`/`DynamicBlock` | `quote-block`/`center-block`/`verse-block`/`special-block`/`dynamic-block` | straightforward 1:1 |
| `ExampleBlock` | `example-block` | |
| `CommentBlock` | `comment-block` | |
| `ExportBlock` | `export-block` | |
| `FixedWidthBlock` | `fixed-width` | |
| `Drawer`/`Logbook`/`Properties` | `drawer`/`drawer` (logbook)/`property-drawer` | |
| `Keyword`/`AffiliatedKeyword`/`CaptionKeyword`/`TblnameKeyword`/`ResultsKeyword`/`PlotKeyword` | `keyword` family | |
| `List`/`ListItem`/`Repeat` | `plain-list`/`item`/`item` | |
| `Table`/`TableRow`/`TableRuleRow`/`TableCell`/`TableEl` | `table`/`table-row`/`table-row` (rule)/`table-cell`/`table` | org-element models a rule as a `table-row` with `:type 'rule`; org-parser gives it a distinct class — reconcile in the walker |
| `BlankLine`/`Comment`/`HorizontalRule`/`Indent` | (structural, mostly droppable or represented minimally) | |
| `Bold`/`Italic`/`Underline`/`StrikeThrough`/`Subscript`/`Superscript` | `bold`/`italic`/`underline`/`strike-through`/`subscript`/`superscript` | confirmed to nest correctly, recurse directly |
| `Verbatim`/`Code` | `verbatim`/`code` | |
| `ExportSnippet`/`Macro`/`InlineBabelCall`/`InlineSourceBlock` | `export-snippet`/`macro`/`inline-babel-call`/`inline-src-block` | |
| `FootnoteReference` | `footnote-reference` | `definition` field is always `None` from this parser — resolve via the promotion pass, not this field |
| `Citation` | `citation` | |
| `PlainLink`/`AngleLink`/`RegularLink` | `link` | split `path` into org-element's `:type`/`:path` on the URI scheme |
| `Target`/`RadioTarget` | `target`/`radio-target` | |
| `PlainText` | (raw string in `contents`, matching `ox-json`'s convention) | after LaTeX/footnote-definition extraction |
| `LineBreak` | `line-break` | |
| `InlineEntity` | `entity` | |
| `CompletionCounter` | `statistics-cookie` | |
| `Timestamp`/`Repeater` | `timestamp` | |
| `Clock` | `clock` | |
| `BabelCall` | `babel-call` | |
| *(none — confirmed absent)* | `latex-fragment`/`latex-environment` | synthesized by the promotion pass, §4.3 |
| *(none — confirmed absent)* | `footnote-definition` | synthesized by the promotion pass, §4.3 |

## Appendix B — math/LaTeX schema recap

From the earlier design discussion, restated here since it directly
constrains §4.3/Appendix A: model math as verbatim capture, not a parsed
expression tree. A `latex-fragment`/`latex-environment` node needs at
minimum `value` (raw source), `display` (`inline`/`block`), and a `dialect`
field defaulting to `latex`. Do not attempt to structurally parse LaTeX
math in this project — if/when structured math is ever needed (search,
non-KaTeX render targets), treat it as an optional derived representation
computed on demand (e.g. via the `texmath` library, which pandoc itself
uses for this) and cached alongside the node, never as a required schema
field.
