# schist data model — design doc (#130)

Status: **agreed design (2026-07-04); slice A implemented (2026-07-06)** —
D2's `schema/frontmatter-contract.json` + both-language conformance tests are
in; slices B and C are still pending. Implementation lands as three PR-sized
slices (A → B → C below). This doc is the contract; each slice PR should link
back here and update the status line.

## Why now

schist operates **two SQLite databases** and **three data layers**, but the
contracts between them live partly in prose (`schema/SCHEMA.md`), partly in
duplicated per-language constants, and partly nowhere. Two incidents show the
cost:

- **#303**: TS and Python normalized concept slugs differently → dangling
  refs after `delete_note` cascades. Fixed by an explicit shared class +
  `schema/concept-slug-parity.json` (#318/#337).
- **REQUIRED_TABLES drift (live today)**: `cli/schist/sqlite_query.py:15`
  requires `{docs, paper_metadata, concept_aliases}`; the TS mirror
  `mcp-server/src/sqlite-reader.ts:112` requires only
  `{paper_metadata, concept_aliases}` — it omits `docs` despite both files
  carrying "keep in sync" comments. Tracked in #339; fixed structurally by
  slice B.

The lesson generalizes: **any contract expressed twice (once per language)
will drift; contracts must be machine-readable, single-sourced, and consumed
by both suites.** The parity fixtures (`frontmatter-parser-parity.json`,
`concept-slug-parity.json`) are the proven template.

## Current state (verified 2026-07-04, main @ 15f8d4f)

| Store | Location | Purpose | git-backed | Versioning today |
|-------|----------|---------|-----------|------------------|
| Vault markdown | `<vault>/…/*.md` | Durable curated knowledge (source of truth) | yes | `schema/SCHEMA.md` prose, "Version 1.0" |
| Vault index `schist.db` | `<vault>/.schist/schist.db` | Derived, disposable query index | no (rebuilt by ingest) | `PRAGMA user_version`: 0 = incomplete ingest, 1 = complete (#244/#328) |
| Memory side DB `agent-state.db` | `~/.openclaw/memory/agent-state.db` (`SCHIST_MEMORY_DB` override) | Fast, ephemeral "fuel station": `agent_memory` (+FTS), `agent_state` (TTL) | no | none |

Key facts the design builds on:

- The index is **disposable**: `schist-ingest` is a full rebuild
  (`cli/schist/ingest.py` re-scans everything; `concept_aliases` is the one
  MCP-written table that survives rebuilds — `cli/schist/schema.sql:115`).
- `user_version` today is a **completion marker**, not a schema version:
  ingest sets 0 before DDL (`ingest.py:329`) and 1 atomically with the data
  commit (`ingest.py:495`); `get_db` heals on `0 + empty docs`
  (`sqlite_query.py:45`).
- The memory DB already has a vault back-reference column
  (`agent_memory.related_doc`, `sqlite-reader.ts:851`) but it is free-text,
  documented as a parameter name only, with no defined semantics.
- `get_context` (`sqlite-reader.ts:783`) never touches the memory DB at any
  depth, so the "fuel station" does not actually fuel session startup.
- The frontmatter field list exists **only as prose tables** in
  `schema/SCHEMA.md`; TS `create_note` writes a fixed field set
  (`tools.ts:1389`), `update_note` patches an allowlist (`tools.ts:153`),
  and Python ingest reads its own list (`ingest.py:360–423` + paper fields)
  — three field lists, no machine check that they agree.

## Decisions

### D1 — Two databases, strictly separated (closes #130's open questions)

1. `search_notes` and `search_memory` stay **strictly separate** tools. No
   cross-search, no merged results: the vault answers "what do we know",
   memory answers "what happened recently around me". A merged search would
   blur provenance (curated vs ephemeral) and force vault-scope ACL logic
   onto the memory DB.
2. Write tools do **not** dual-write. `create_note` does not auto-create
   memory entries and `add_memory` does not create notes. Agents (or skills
   like `/learn` and `/handoff`) decide when something graduates from memory
   to vault; the data layer stays predictable. The bridge is the
   back-reference (D4), not a write hook.
3. `SCHIST_MEMORY_DB` stays **outside the vault** (default
   `~/.openclaw/memory/`). Putting it at `<vault>/.schist/memory.db` would
   make ephemeral, un-reviewed content sweepable by vault tooling and backups
   (the #309 class of artifact-in-vault problems) and would entangle memory
   writes with vault sync/locking.

### D2 — Document model: machine-readable frontmatter contract (slice A)

Add `schema/frontmatter-contract.json`: one array of field descriptors —

```json
{ "field": "confidence", "type": "enum:low|medium|high",
  "appliesTo": ["documents"], "writtenBy": ["create_note", "update_note"],
  "readBy": ["ingest", "parseNote"], "invalid": "coerce-null" }
```

- Both suites load it: a TS test asserts `create_note`'s written fields and
  `PATCHABLE_FRONTMATTER_KEYS` match the contract; a Python test asserts
  ingest's read set and coercion rules (`confidence` → NULL on invalid, etc.)
  match. Drift in either language fails that language's CI.
- `schema/SCHEMA.md` keeps the prose (human spec) and gains a line per table
  pointing at the JSON as the enforced source of truth.
- Scope: document + concept + paper fields, including nested
  `verification.*`. NOT a general YAML schema — only the fields the two
  parsers actually handle, which is what can drift.

### D3 — Index model: schema version + shared required-tables (slice B)

1. **Generalize `user_version`** rather than adding a second mechanism:
   `user_version = 0` while an ingest is in flight (unchanged heal
   semantics), `user_version = INDEX_SCHEMA_VERSION` on completion. Today
   `INDEX_SCHEMA_VERSION = 1`, so existing DBs are already valid; the value
   is bumped only on DDL changes to `schema.sql`. A reader that finds
   `0 < user_version != INDEX_SCHEMA_VERSION` treats the DB as stale and
   forces a rebuild (the index is disposable — **rebuild IS the migration
   path**; no ALTER migrations). This is deliberately collision-free with
   the #244 marker because the marker and the version become one value:
   "complete at schema version N".

   **Deployment-skew rule**: the TS stale-version path MUST go through the
   existing `ensureSchemaCurrent` rebuild-once → recheck → typed-error
   pattern (`sqlite-reader.ts:236–301`) — a newer mcp-server paired with an
   older installed `schist-ingest` still stamps the old version after a
   rebuild, and without the recheck-then-error the server would either
   rebuild the vault index on every tool call or loop uninformatively. The
   Python side is skew-free by construction: `_run_ingest` imports
   `schist.ingest` in-process, so reader and writer always share a version.
2. **Single source for the table contract**: `schema/index-contract.json`
   holding `{ "schemaVersion": 1, "tables": [...], "requiredTables": [...],
   "rebuildSurvivors": ["concept_aliases"] }`. `sqlite_query.py` and
   `sqlite-reader.ts` both load it, and the per-language `REQUIRED_TABLES`
   constants are deleted (TS `REQUIRED_DOCS_COLUMNS` survives unless the
   contract also carries column lists — pre-marker DBs are exempt from the
   version check, so column drift on them is caught only by the column
   check). **Packaging mechanism** (repo-root files cannot be Python
   package-data, and the npm package ships only `dist/`): use the existing
   `default.yaml` pattern — relative-path load in a repo checkout with a
   baked-in mirror in each component, pinned by a drift test
   (`tools.ts:26–66` precedent); the mirror-vs-schema/ parity test is what
   makes the single source real. A parity test in each suite additionally
   asserts the loaded contract matches what `schema.sql` actually creates.
3. Fixes #339 (the `docs` drift) as a side effect, in the direction of the
   CLI's stricter set — a DB without `docs` is unusable for every read path.

### D4 — Memory contract: formalize the fuel station (slice C)

1. **Back-references**: `agent_memory.related_doc` exists today but is
   documented as a name only, with no semantics
   (`docs/cross-project-memory.md`). Define it as "a vault note id
   (`notes/….md`)"; `add_memory` validates the *shape* (id-like string)
   but not existence (memory must be writable when the vault is unavailable
   — no FK, no cross-DB check). `search_memory` already returns it; the new
   work is the shape validation, the documented semantics, and carrying it
   into `get_context`'s memory block so agents can hop memory → note.
2. **`get_context` surfaces memory**: at `standard` and `full` depth, append
   a `recentMemory` block — the owner's N (default 5) most recent
   `agent_memory` entries (id, date, entry_type, 100-char content snippet,
   related_doc). **Owner resolution**: `get_context` has no identity today
   (`tools.ts:2350` takes only `depth`/`verbose`); resolve from
   `SCHIST_AGENT_ID`, and on multi-owner servers (`SCHIST_ALLOWED_AGENTS`)
   accept an optional `owner` arg validated against the allowlist, falling
   back to `SCHIST_AGENT_ID` when omitted. Clearly namespaced so vault
   context and ephemeral memory are visually distinct; `minimal` stays
   counts-only. Memory-DB-unavailable degrades to an absent block, never an
   error (get_context must not break when the fuel station is missing).
3. **Decision boundary in server instructions**: extend
   `mcp-server/src/server-instructions.ts` with the one-paragraph rule:
   memory = frequent/small/session-scoped (decisions made, blockers hit,
   state); vault note = durable/curated/cross-session knowledge; graduate
   memory → note when it survives its session. (Today both are lumped as
   "persist new knowledge".)
4. No TTL on `agent_memory` for now — TTL stays on `agent_state` only.
   Memory entries are the audit trail `/recall` depends on; retention policy
   is a curation decision, not a data-layer default.

## Implementation slices

| Slice | Content | Touches | Acceptance |
|-------|---------|---------|------------|
| **A** | `schema/frontmatter-contract.json` + conformance tests both sides + SCHEMA.md pointers | schema/, both test suites | a field added to one language without the contract fails that suite |
| **B** | `schema/index-contract.json`, `INDEX_SCHEMA_VERSION`, generalized `user_version` semantics, delete per-language REQUIRED_TABLES (fixes #339) | schema/, `ingest.py`, `sqlite_query.py`, `sqlite-reader.ts` | stale-schema DB triggers rebuild; contract/schema.sql parity test; single required-tables source |
| **C** | `related_doc` shape validation + docs, `recentMemory` in get_context (standard+), server-instructions boundary text | `sqlite-reader.ts`, `tools.ts`, `server-instructions.ts`, `docs/cross-project-memory.md` | get_context shows owner-scoped recent memory; degrades gracefully without memory DB |

Order matters: A and B are prerequisites for shape-ossifying features
(#97/#74/#91/#68) and launch work (#96/#92/#94); C is independent but small.

## Non-goals

- No merged notes+memory search; no dual-write hooks (D1).
- No SQL migration framework for the index — rebuild is the migration.
- No memory-entry TTL/retention automation (D4.4).
- No confidence-weighted memory search ranking (#130 proposal item 4):
  `search_memory` already filters on confidence; changing result *ordering*
  is a retrieval-quality experiment, not a data-model contract, and can be
  revisited after slice C ships.
- No change to `vault.yaml` `vault_version` (separate config contract).
