# PR 6 Plan — `list_concepts` + `list_domains` cursor adoption (Issue #50)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt cursor pagination on `list_concepts` and `list_domains` — the last two vault-DB list tools in the Issue #50 rollout. Add `limit: 100 / cap: 500` default to `list_domains` (currently unbounded). Correct a docs-only input-shape mismatch in the spec's cursor-inputs table. Bundle a fresh audit snapshot.

**Architecture:** Follows the same three-layer pattern as PR 4 (`search_notes`) — the closest structural twin, since neither tool uses verbose mode. (1) SQL layer: `listConcepts` gains `offset` param + `c.slug ASC` tiebreaker; `listDomains` gains `limit`/`offset` params (already deterministic — no new tiebreaker needed). (2) Type layer: add `ListConceptsResponse` and `ListDomainsResponse` interfaces to `types.ts`. (3) Tool-handler layer: both handlers become the cursor-protocol wiring point (`canonicalizeQueryHash` → cursor decode/refusal → SQL → `recordIssued`+`issueCursor` if capped → response shape). No verbose mode for either tool (spec §"Reason-string adopters" explicitly excludes all `list_*` tools).

**Tech Stack:** TypeScript (strict, ESM, Node ≥20). No new runtime dependencies. Jest 30 + ts-jest ESM preset.

**Tracks:** [yibeichan/schist#50](https://github.com/yibeichan/schist/issues/50) (PR 6 of 8-PR rollout). Spec contract: `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md`.

**Baseline tests:** 16 suites / 286 tests green (as of 2026-05-20).

---

## Spec → task mapping

| Spec section | PR 6 task |
|---|---|
| Spec doc-only correction: `list_concepts` cursor-inputs table row (Task 6.0) | Task 6.0 |
| Cursor table row: `list_concepts` ORDER BY tiebreaker `edgeCount DESC, c.slug ASC` | Task 6.1 |
| Cursor table row: `list_domains` already-deterministic, just needs LIMIT/OFFSET | Task 6.2 |
| Default limits: `list_concepts` limit 50 / cap 200 (unchanged); `list_domains` add limit 100 / cap 500 | Task 6.1, 6.2 |
| Response-shape types: `ListConceptsResponse`, `ListDomainsResponse` | Task 6.3 |
| `list_concepts` handler — cursor pipeline wiring | Task 6.4 |
| `list_domains` handler — cursor pipeline wiring | Task 6.5 |
| `tool-registry.ts` schema updates (cursor + limit description for both tools) | Task 6.6 |
| `index.ts` call-sites — verify propagation (read-only check, no change expected) | Task 6.6 |
| Audit re-measurement | Task 6.7 |

---

## Spec / handler mismatch resolution (Task B)

### The mismatch

The spec's cursor-inputs table (line 242 of `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md`) reads:

```
| `list_concepts` | `q?`, `domain?`, `limit`, owner | ...
```

The deployed handler (`tools.ts` line 555–563) and SQLite reader (`sqlite-reader.ts` line 175–177) accept:

```typescript
{ tags?: string[]; search?: string; limit?: number }
```

The registry schema (`tool-registry.ts` line 205–214) also exposes `tags` and `search`, not `q` and `domain`.

### Decision (locked)

**The handler is canonical.** The spec row is a documentation error written before the handler existed (the args `q` and `domain` were a draft-time placeholder; the actual implementation chose `tags` and `search`). Correcting the handler argument names would be a breaking change for any existing agent. The spec row is corrected in Task 6.0 as a docs-only commit bundled into PR 6.

**queryHash for `list_concepts` binds to:** `(tags?, search?, limit, owner)` — i.e., all args that the actual deployed handler accepts, minus `cursor` (auto-excluded by `canonicalizeQueryHash`).

### Spec correction (verbatim replacement for line 242)

**Before:**
```
| `list_concepts` | `q?`, `domain?`, `limit`, owner | `ORDER BY edgeCount DESC` (line 186). | `ORDER BY edgeCount DESC, c.slug ASC`. | Existing SQL + `, slug ASC` tiebreaker + `LIMIT :limit OFFSET :offset`. |
```

**After:**
```
| `list_concepts` | `tags?`, `search?`, `limit`, owner | `ORDER BY edgeCount DESC` (line 204 in sqlite-reader.ts). | `ORDER BY edgeCount DESC, c.slug ASC`. | Existing SQL + `, slug ASC` tiebreaker + `LIMIT :limit OFFSET :offset`. |
```

Also update the `list_domains` line reference: spec says "line 525" but actual `listDomains` is at line 561 of `sqlite-reader.ts`.

---

## Files

### `mcp-server/src/sqlite-reader.ts` — exact lines + diff sketches

#### `listConcepts` (currently lines 175–218)

**Before (relevant tail of function, lines 203–205):**
```typescript
    if (where.length > 0) sql += ` WHERE ${where.join(" AND ")}`;
    sql += ` GROUP BY c.slug ORDER BY edgeCount DESC LIMIT ?`;
    params.push(limit);
```

**After:**
```typescript
    if (where.length > 0) sql += ` WHERE ${where.join(" AND ")}`;
    // c.slug ASC tiebreaker is required for stable LIMIT/OFFSET pagination.
    sql += ` GROUP BY c.slug ORDER BY edgeCount DESC, c.slug ASC LIMIT ? OFFSET ?`;
    params.push(limit, offset);
```

**Signature change (line 175–177):**

**Before:**
```typescript
export function listConcepts(
  vaultRoot: string,
  opts?: { tags?: string[]; search?: string; limit?: number }
): Concept[] {
  const db = openDb(vaultRoot);
  try {
    const limit = opts?.limit ?? 50;
```

**After:**
```typescript
export function listConcepts(
  vaultRoot: string,
  opts?: { tags?: string[]; search?: string; limit?: number; offset?: number }
): Concept[] {
  const db = openDb(vaultRoot);
  try {
    const limit = opts?.limit ?? 50;
    const offset = opts?.offset ?? 0;
```

#### `listDomains` (currently lines 561–578)

**Before:**
```typescript
export function listDomains(vaultRoot: string): Domain[] {
  let db: Database.Database | undefined;
  try {
    db = openDb(vaultRoot);
    const rows = db.prepare("SELECT * FROM domains ORDER BY parent_slug NULLS FIRST, slug").all() as Record<string, unknown>[];
```

**After:**
```typescript
export function listDomains(
  vaultRoot: string,
  opts?: { limit?: number; offset?: number }
): Domain[] {
  let db: Database.Database | undefined;
  try {
    db = openDb(vaultRoot);
    const limit = opts?.limit ?? 100;
    const offset = opts?.offset ?? 0;
    // ORDER BY parent_slug NULLS FIRST, slug is already deterministic (slug is
    // unique PK). No tiebreaker needed — spec confirms this.
    const rows = db.prepare(
      "SELECT * FROM domains ORDER BY parent_slug NULLS FIRST, slug LIMIT ? OFFSET ?"
    ).all(limit, offset) as Record<string, unknown>[];
```

---

### `mcp-server/src/tools.ts` — exact lines + diff sketch

#### `list_concepts` handler (currently lines 555–564)

**Before:**
```typescript
export async function list_concepts(
  vaultRoot: string,
  args: { tags?: string[]; search?: string; limit?: number }
): Promise<unknown> {
  try {
    return sqliteReader.listConcepts(vaultRoot, args);
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
  }
}
```

**After (full replacement):**
```typescript
/**
 * list_concepts tool handler. Runs the cursor pipeline:
 *
 *   canonicalizeQueryHash → (cursor decode + binding OR identical-query
 *   refusal) → SQL fetch (limit+1, with slug ASC tiebreaker in sqlite-reader)
 *   → recordIssued + issueCursor on capped results → { concepts, cursor? }.
 *
 * No verbose mode — per spec, `list_*` tools are excluded from verbose.
 * queryHash binds to (tags?, search?, limit, owner) — see spec §"Cursor
 * adoption table" and PR 6 plan §"Spec/handler mismatch resolution".
 *
 * Spec: docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md
 */
export async function list_concepts(
  vaultRoot: string,
  args: { tags?: string[]; search?: string; limit?: number; cursor?: string }
): Promise<ListConceptsResponse | ToolError> {
  const TOOL_NAME = "list_concepts" as const;

  // Step 1: canonicalizeQueryHash. No per-call owner arg on list_concepts.
  const activeOwner = process.env.SCHIST_AGENT_NAME ?? process.env.SCHIST_AGENT_ID ?? "";
  const ch = canonicalizeQueryHash(args as Record<string, unknown>, activeOwner);
  if (!ch.ok) return ch.error;
  const queryHash = ch.queryHash;

  // Step 2: Cursor decoding + queryHash binding check.
  let offset = 0;
  let consumingCursor = false;
  if (typeof args.cursor === "string" && args.cursor.length > 0) {
    const d = decodeCursor(args.cursor, TOOL_NAME);
    if (!d.ok) return d.error;
    if (d.queryHash !== queryHash) {
      return {
        error: "CURSOR_INVALID_SIGNATURE",
        message: "cursor was issued for a different query — restart pagination from page 1",
      };
    }
    offset = d.offset;
    consumingCursor = true;
  }

  // Step 3: Identical-query refusal (no-cursor path only). No verbose mode.
  if (!consumingCursor) {
    const refusal = checkRefusal({
      tool: TOOL_NAME,
      queryHash,
      owner: activeOwner,
      verboseEnabled: false,
    });
    if (refusal.refuse) return refusal.error;
  }

  // Step 4: SQL fetch with limit + 1. Default 50, cap 200.
  const requested = args.limit;
  const effectiveLimit =
    requested === undefined || requested === null || requested <= 0
      ? 50
      : Math.min(requested, 200);

  let concepts: import("./types.js").Concept[];
  try {
    concepts = sqliteReader.listConcepts(vaultRoot, {
      ...args,
      limit: effectiveLimit + 1,
      offset,
    });
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
  }

  // Step 5: hasMore detection + cursor issuance.
  const hasMore = concepts.length > effectiveLimit;
  if (hasMore) concepts = concepts.slice(0, effectiveLimit);

  const response: ListConceptsResponse = { concepts };
  if (hasMore) {
    const cursor = issueCursor({
      tool: TOOL_NAME,
      queryHash,
      offset: offset + effectiveLimit,
    });
    response.cursor = cursor;
    recordIssued({ tool: TOOL_NAME, queryHash, owner: activeOwner, verboseEnabled: false });
  }

  return response;
}
```

#### `list_domains` handler (currently lines 878–887)

**Before:**
```typescript
export async function list_domains(
  vaultRoot: string,
  _args: Record<string, never>
): Promise<unknown> {
  try {
    return sqliteReader.listDomains(vaultRoot);
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
  }
}
```

**After (full replacement):**
```typescript
/**
 * list_domains tool handler. Runs the cursor pipeline:
 *
 *   canonicalizeQueryHash → (cursor decode + binding OR identical-query
 *   refusal) → SQL fetch (limit+1) → recordIssued + issueCursor on capped
 *   results → { domains, cursor? }.
 *
 * No verbose mode. ORDER BY parent_slug NULLS FIRST, slug is already
 * deterministic (slug unique). Default limit 100, cap 500 (per spec Default
 * limits table — unbounded was a footgun for large vaults).
 *
 * Spec: docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md
 */
export async function list_domains(
  vaultRoot: string,
  args: { limit?: number; cursor?: string }
): Promise<ListDomainsResponse | ToolError> {
  const TOOL_NAME = "list_domains" as const;

  // Step 1: canonicalizeQueryHash.
  const activeOwner = process.env.SCHIST_AGENT_NAME ?? process.env.SCHIST_AGENT_ID ?? "";
  const ch = canonicalizeQueryHash(args as Record<string, unknown>, activeOwner);
  if (!ch.ok) return ch.error;
  const queryHash = ch.queryHash;

  // Step 2: Cursor decode + binding check.
  let offset = 0;
  let consumingCursor = false;
  if (typeof args.cursor === "string" && args.cursor.length > 0) {
    const d = decodeCursor(args.cursor, TOOL_NAME);
    if (!d.ok) return d.error;
    if (d.queryHash !== queryHash) {
      return {
        error: "CURSOR_INVALID_SIGNATURE",
        message: "cursor was issued for a different query — restart pagination from page 1",
      };
    }
    offset = d.offset;
    consumingCursor = true;
  }

  // Step 3: Identical-query refusal. No verbose mode.
  if (!consumingCursor) {
    const refusal = checkRefusal({
      tool: TOOL_NAME,
      queryHash,
      owner: activeOwner,
      verboseEnabled: false,
    });
    if (refusal.refuse) return refusal.error;
  }

  // Step 4: SQL fetch with limit + 1. Default 100, cap 500.
  const requested = args.limit;
  const effectiveLimit =
    requested === undefined || requested === null || requested <= 0
      ? 100
      : Math.min(requested, 500);

  let domains: import("./types.js").Domain[];
  try {
    domains = sqliteReader.listDomains(vaultRoot, {
      limit: effectiveLimit + 1,
      offset,
    });
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
  }

  // Step 5: hasMore detection + cursor issuance.
  const hasMore = domains.length > effectiveLimit;
  if (hasMore) domains = domains.slice(0, effectiveLimit);

  const response: ListDomainsResponse = { domains };
  if (hasMore) {
    const cursor = issueCursor({
      tool: TOOL_NAME,
      queryHash,
      offset: offset + effectiveLimit,
    });
    response.cursor = cursor;
    recordIssued({ tool: TOOL_NAME, queryHash, owner: activeOwner, verboseEnabled: false });
  }

  return response;
}
```

**Import addition needed at top of tools.ts** — add `ListConceptsResponse, ListDomainsResponse` to the existing `import type { ... } from "./types.js"` line. Existing imports from `"./protocol/index.js"` (`canonicalizeQueryHash`, `decodeCursor`, `issueCursor`, `recordIssued`, `checkRefusal`) are already present (added in PR 4/5).

---

### `mcp-server/src/tool-registry.ts` — schema diffs

#### `list_domains` entry (currently lines 65–69)

**Before:**
```typescript
    {
      name: "list_domains",
      description: "List research domain taxonomy.",
      inputSchema: { type: "object" as const, properties: {} },
    },
```

**After:**
```typescript
    {
      name: "list_domains",
      description: "List research domain taxonomy. Paginated: when results are capped, the response includes a `cursor` token — echo it back on the next call to advance. Identical queries within 300s without a cursor are refused with CURSOR_REQUIRED.",
      inputSchema: {
        type: "object" as const,
        properties: {
          limit: { type: "number", description: "Default 100, capped at 500." },
          cursor: { type: "string", description: "Opaque pagination cursor returned by a prior call. Echo verbatim; do not modify." },
        },
      },
    },
```

#### `list_concepts` entry (currently lines 204–215)

**Before:**
```typescript
    {
      name: "list_concepts",
      description: "List all concepts in the knowledge graph",
      inputSchema: {
        type: "object" as const,
        properties: {
          tags: { type: "array", items: { type: "string" } },
          search: { type: "string" },
          limit: { type: "number" },
        },
      },
    },
```

**After:**
```typescript
    {
      name: "list_concepts",
      description: "List all concepts in the knowledge graph. Paginated: when results are capped, the response includes a `cursor` token — echo it back on the next call to advance. Identical queries within 300s without a cursor are refused with CURSOR_REQUIRED.",
      inputSchema: {
        type: "object" as const,
        properties: {
          tags: { type: "array", items: { type: "string" } },
          search: { type: "string" },
          limit: { type: "number", description: "Default 50, capped at 200." },
          cursor: { type: "string", description: "Opaque pagination cursor returned by a prior call. Echo verbatim; do not modify." },
        },
      },
    },
```

---

### `mcp-server/src/types.ts` — new response shapes

Add after `SearchNotesResponse` (line 46), before `QueryGraphResponse` (line 48):

```typescript
export interface ListConceptsResponse {
  concepts: Concept[];
  /** Opaque cursor token for the next page; absent when this is the last page. */
  cursor?: string;
}

export interface ListDomainsResponse {
  domains: Domain[];
  /** Opaque cursor token for the next page; absent when this is the last page. */
  cursor?: string;
}
```

Note: `Domain` interface must be confirmed present in `types.ts`. If it only lives in `sqlite-reader.ts`, move or re-export it into `types.ts` as part of this task. (Quick check: `grep -n "^export interface Domain" mcp-server/src/types.ts mcp-server/src/sqlite-reader.ts`.)

---

### `mcp-server/src/index.ts` — call-site verification

Current call sites at lines 101–102 and 116–117:

```typescript
case "list_concepts":
  result = await list_concepts(vaultRoot, toolArgs as Parameters<typeof list_concepts>[1]);
  break;
// ...
case "list_domains":
  result = await list_domains(vaultRoot, toolArgs as Parameters<typeof list_domains>[1]);
  break;
```

These use `Parameters<typeof handler>[1]` type casting — they will automatically pick up the new `cursor?` and `limit?` fields once `tools.ts` is updated. **No change needed to `index.ts`** — the `Parameters<>` cast means the new optional fields are transparently passed through.

---

### `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md` — spec row correction

**Line 242 correction** (Task 6.0 commit, docs-only):

Change `q?`, `domain?` → `tags?`, `search?` in the `queryHash includes` cell, and update the sqlite-reader line reference:

| Before | After |
|--------|-------|
| `` `q?`, `domain?`, `limit`, owner `` | `` `tags?`, `search?`, `limit`, owner `` |
| `ORDER BY edgeCount DESC` (line 186) | `ORDER BY edgeCount DESC` (line 204 in sqlite-reader.ts) |

**Line 243 correction** (same commit):

| Before | After |
|--------|-------|
| `ORDER BY parent_slug NULLS FIRST, slug` (line 525) | `ORDER BY parent_slug NULLS FIRST, slug` (line 565 in sqlite-reader.ts) |

**Self-review checklist update** (line ~496):

```
  - PR 6 → "list_concepts" + "list_domains" rows + Default limits.
```

Append: `(spec row corrected: list_concepts inputs are tags?/search? not q?/domain?)`

---

### `mcp-server/tests/list-concepts-tool.test.ts` (new)

Mirror structure of `search-notes-tool.test.ts`. Required test cases:

1. `makeVault()` helper — creates minimal `concepts` + `edges` tables (no FTS needed).
2. `seed(vaultRoot, n)` — inserts N concepts with known slugs (`c-000` … `c-NNN`) so pagination order is predictable.
3. **canonicalize errors** — unhashable arg (NaN limit) → `INVALID_ARG`.
4. **cursor decode errors** — malformed cursor → `CURSOR_INVALID_SIGNATURE`; wrong-tool cursor → `CURSOR_WRONG_TOOL`.
5. **queryHash binding** — cursor issued with wrong queryHash → `CURSOR_INVALID_SIGNATURE` with "different query" message.
6. **identical-query refusal** — same (tool, queryHash, owner) within TTL without cursor → `CURSOR_REQUIRED`.
7. **pagination** — seed 15 concepts, call at `limit: 5` → page 1 has 5 concepts + cursor; page 2 advances (no overlap); page 3 has ≤5 + no cursor.
8. **cap enforcement** — `limit: 9999` → capped at 200.
9. **limit: 0 → default 50** — treated as default.
10. **empty results** — empty vault → `{ concepts: [] }`, no cursor.
11. **tiebreaker stability** — seed 6 concepts with equal edgeCount (0); paginate at limit=3 across two pages; assert no overlap and page1+page2 covers all 6 slugs in `c.slug ASC` order.
12. **filter params** — `tags: ["neural"]` only returns matching concepts; `search: "foo"` filters correctly.
13. **negative limit → default** — `limit: -1` collapses to default 50 (mirrors `search-notes-tool.test.ts:246`).
14. **normalizeError fallthrough** — mock `sqliteReader.listConcepts` to throw; assert response is `{ error: "INGEST_ERROR", message: ... }` shape.
15. **empty owner** — unset `SCHIST_AGENT_ID` and `SCHIST_AGENT_NAME`; assert handler still returns valid response (common dev/CI scenario).

Estimated: ~21 tests.

---

### `mcp-server/tests/list-domains-tool.test.ts` (new)

Mirror structure. Required test cases:

1. `makeVault()` helper — creates minimal `domains` table.
2. `seed(vaultRoot, n)` — inserts N domains with known slugs.
3. **canonicalize errors** — NaN limit → `INVALID_ARG`.
4. **cursor decode errors** — malformed/wrong-tool cursor.
5. **queryHash binding** — wrong-hash cursor → `CURSOR_INVALID_SIGNATURE`.
6. **identical-query refusal** — `CURSOR_REQUIRED` on repeat call without cursor.
7. **pagination** — seed 120 domains, call at default limit (100) → page 1 has 100 + cursor; page 2 has 20 + no cursor.
8. **cap enforcement** — `limit: 9999` → capped at 500.
9. **limit: 0 → default 100**.
10. **empty results** — empty vault → `{ domains: [] }`, no cursor.
11. **tiebreaker stability** (already deterministic) — seed 10 parent + 10 child domains; paginate at limit=5 across 4 pages; assert no overlap.
12. **negative limit → default** — `limit: -1` collapses to default 100.
13. **normalizeError fallthrough** — mock `sqliteReader.listDomains` to throw; assert response is `{ error: "INGEST_ERROR", ... }` shape.
14. **empty owner** — unset `SCHIST_AGENT_ID` and `SCHIST_AGENT_NAME`; assert handler still returns valid response.

Estimated: ~18 tests.

---

### `docs/superpowers/specs/audit-2026-05-20-mcp-response-sizes-pr6.md` (new)

Audit snapshot measuring `list_concepts` and `list_domains` response sizes before and after, following the format established by `audit-2026-05-17-mcp-response-sizes-pr4.md`. The audit script at `scripts/audit_mcp_response_sizes.ts` must be updated to recognize the new `{ concepts }` and `{ domains }` wrapper shapes alongside the existing `{ results }` and `{ entries }` shapes.

---

## Design decisions

### 1. queryHash binding for `list_concepts` (the resolved mismatch)

**Decision:** `queryHash = sha256(canonicalize({ tags?, search?, limit }, activeOwner))`.

The handler accepts `tags?` + `search?`. The spec's `q?` + `domain?` were a documentation error. The spec row is corrected in Task 6.0 (docs-only). The handler argument names are **not changed** (breaking change for any live agent). This is locked for all PR 6 cursor tests.

### 2. Default limit + cap for `list_domains`

Per spec Default limits table (line 397):

- **Default:** `limit: 100` (was unbounded — footgun for large vaults)
- **Cap:** `500`

`list_concepts` default/cap unchanged: `50` / `200`.

### 3. Active owner derivation

Both tools use `process.env.SCHIST_AGENT_NAME ?? process.env.SCHIST_AGENT_ID ?? ""` — the same env-var chain as `search_notes` (minus its leading `args.owner ??` branch, since list_* don't accept per-call owner). This keeps identity resolution uniform across all cursor-adopting tools — same agent always hashes to the same `owner` regardless of which tool it called. Locked by eng review 2026-05-20.

### 4. ORDER BY tiebreakers

- `list_concepts`: current SQL ends with `ORDER BY edgeCount DESC` (line 204 of sqlite-reader.ts). Add `, c.slug ASC`. Slug is the concept's unique identifier — stable deterministic tiebreaker.
- `list_domains`: current SQL ends with `ORDER BY parent_slug NULLS FIRST, slug` (line 565 of sqlite-reader.ts). `slug` is the primary key — already deterministic. **No change to ORDER BY clause needed.**

### 5. No verbose mode

Both tools are explicitly excluded from verbose mode per spec §"Reason-string adopters":
> "All `list_*` and `query_graph` tools — their response shape is the natural unit; 'verbose mode' doesn't make sense semantically."

`verboseEnabled` is always `false` in `recordIssued` calls. No `parseVerbose` step needed.

### 6. Response shape — breaking change assessment

- `list_concepts` **currently returns** a bare `Concept[]`. After PR 6 it returns `{ concepts: Concept[], cursor?: string }`. This is a breaking change for callers doing `r[0]` instead of `r.concepts[0]`. Consistent with `search_notes` (PR 4), which had the same breaking change. Document in CHANGELOG under `### Changed`.
- `list_domains` **currently returns** a bare `Domain[]`. Same treatment.
- Both changes are listed in the spec's "Compatibility / migration" section scope for PR 8 migration steps.

### 7. `Domain` type location

Verify whether `Domain` is exported from `types.ts` or only defined in `sqlite-reader.ts`. If only in `sqlite-reader.ts`, move/re-export to `types.ts` as part of Task 6.3 to avoid circular imports in the new response-shape interfaces.

---

## Task graph

Tasks 6.1 and 6.2 are independent SQL-layer changes and can run in parallel. Task 6.3 (types) is independent of both. Tasks 6.4 and 6.5 (handlers) each depend on their respective SQL task (6.1→6.4, 6.2→6.5) and on 6.3. Task 6.6 (registry) depends on 6.4+6.5 (to verify arg names match). Task 6.7 (audit) depends on all prior tasks.

```
6.0 (spec doc-only) ──────────────────────────────────────────────────┐
6.1 (sqlite-reader: listConcepts offset+tiebreaker) ─┐                │
6.2 (sqlite-reader: listDomains limit+offset) ────────┤               │
6.3 (types.ts: ListConceptsResponse/ListDomainsResponse) ─┐           │
                                                           ▼           │
6.4 (tools.ts: list_concepts handler) ◄── 6.1 + 6.3      │           │
6.5 (tools.ts: list_domains handler) ◄─── 6.2 + 6.3      │           │
                                          ▼               │           │
6.6 (tool-registry.ts + index.ts verify) ◄─── 6.4+6.5    │           │
                                          │               │           │
6.7 (audit doc + CHANGELOG) ◄────────────┘               │           │
                                                          └── 6.4+6.5 ┘
```

**Parallel pairs:** (6.0), (6.1, 6.2, 6.3) can all be submitted in a single parallel batch. Then (6.4, 6.5) in a second batch. Then 6.6, then 6.7.

---

## Task 6.0: Spec doc-only correction

**Files:**
- Modify: `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md`

**Steps:**

- [ ] Correct line 242: replace `` `q?`, `domain?` `` → `` `tags?`, `search?` ``; update line ref `(line 186)` → `(line 204 in sqlite-reader.ts)`.
- [ ] Correct line 243: update line ref `(line 525)` → `(line 565 in sqlite-reader.ts)`.
- [ ] Update PR 6 checklist entry (~line 496) to note the spec correction.
- [ ] Commit:

```bash
git commit -m "$(cat <<'EOF'
spec(#50): correct list_concepts cursor-inputs table row (PR 6 doc fix)

The spec's cursor-adoption table wrote list_concepts inputs as `q?`,
`domain?` — a draft-time placeholder. The deployed handler
(tools.ts:555) and sqlite-reader accept `tags?`, `search?`. Correcting
the spec row so PR 6 implementers have an accurate reference.

Also updates sqlite-reader line references for listConcepts (186→204)
and listDomains (525→565) to match current code after PRs 3-5 landed.

Refs #50.
EOF
)"
```

---

## Task 6.1: SQL layer — `listConcepts` offset + tiebreaker

**Files:**
- Modify: `mcp-server/src/sqlite-reader.ts` (lines 175–218)
- Create: `mcp-server/tests/list-concepts-sql.test.ts` (or extend `sqlite-reader.test.ts`)

**Steps:**

- [ ] Write failing tests: offset pagination + tiebreaker stability for `listConcepts`.
- [ ] Extend `listConcepts` signature with `offset?: number` (default 0).
- [ ] Change ORDER BY from `edgeCount DESC` → `edgeCount DESC, c.slug ASC`.
- [ ] Change `LIMIT ?` → `LIMIT ? OFFSET ?`; push `offset` to params.
- [ ] Run tests; verify new tests pass + all existing pass.
- [ ] Commit.

---

## Task 6.2: SQL layer — `listDomains` limit + offset

**Files:**
- Modify: `mcp-server/src/sqlite-reader.ts` (lines 561–578)

**Steps:**

- [ ] Write failing tests: `listDomains` with `limit` and `offset` opts.
- [ ] Extend `listDomains` signature with `opts?: { limit?: number; offset?: number }`.
- [ ] Default `limit = 100`, `offset = 0`.
- [ ] Change static SQL string to parameterized `LIMIT ? OFFSET ?`; pass `limit, offset`.
- [ ] Run tests; verify new tests pass + all existing pass.
- [ ] Commit.

---

## Task 6.3: Types — `ListConceptsResponse` + `ListDomainsResponse`

**Files:**
- Modify: `mcp-server/src/types.ts`

**Steps:**

- [ ] Check whether `Domain` interface is in `types.ts`. If not, add it (or re-export from sqlite-reader).
- [ ] Add `ListConceptsResponse` and `ListDomainsResponse` interfaces after `SearchNotesResponse` (line 46).
- [ ] Run `npm run build` to verify typecheck.
- [ ] Commit (can bundle with Task 6.6 or standalone — standalone preferred so Task 6.4/6.5 can import it cleanly).

---

## Task 6.4: `list_concepts` handler — cursor pipeline

**Files:**
- Modify: `mcp-server/src/tools.ts` (lines 555–564)
- Create: `mcp-server/tests/list-concepts-tool.test.ts`

**Steps:**

- [ ] Write failing tests (all ~18 cases listed in the Files section above).
- [ ] Update `import type { ..., ListConceptsResponse, ListDomainsResponse } from "./types.js"` at top of tools.ts.
- [ ] Replace `list_concepts` function body with the cursor pipeline as sketched above.
- [ ] Run tests; verify new tests pass + all existing pass (watch for the response-shape change on `list_concepts` in `tools.test.ts` or `tool-listing.test.ts` if they snapshot the return value).
- [ ] Commit.

---

## Task 6.5: `list_domains` handler — cursor pipeline

**Files:**
- Modify: `mcp-server/src/tools.ts` (lines 878–887)
- Create: `mcp-server/tests/list-domains-tool.test.ts`

**Steps:**

- [ ] Write failing tests (all ~15 cases listed in the Files section above).
- [ ] Replace `list_domains` function body with the cursor pipeline as sketched above.
- [ ] Run tests; verify new tests pass.
- [ ] Commit.

---

## Task 6.6: `tool-registry.ts` schema updates + `index.ts` verification

**Files:**
- Modify: `mcp-server/src/tool-registry.ts`

**Steps:**

- [ ] Update `list_domains` entry: add `limit` + `cursor` to `inputSchema.properties`; update description.
- [ ] Update `list_concepts` entry: add `cursor` to `inputSchema.properties`; update description; add `description` field to `limit`.
- [ ] Verify `index.ts` call sites (lines 101–102, 116–117) — no changes needed; `Parameters<>` cast handles new optional args.
- [ ] Run `npm run build` + `npm test`.
- [ ] Commit.

---

## Task 6.7: Audit doc + CHANGELOG

**Files:**
- Create: `docs/superpowers/specs/audit-2026-05-20-mcp-response-sizes-pr6.md`
- Modify: `scripts/audit_mcp_response_sizes.ts` (recognize `{ concepts }` and `{ domains }` wrapper shapes)
- Modify: `CHANGELOG.md` (add response-shape breaking changes under `### Changed`)

**Steps:**

- [ ] Update audit script to handle new wrapper shapes.
- [ ] **REGRESSION TEST (mandatory):** update `mcp-server/tests/audit-script.test.ts` to assert `{ concepts: [...] }` and `{ domains: [...] }` wrapper shapes are recognized alongside existing `{ results }` / `{ entries }` shapes. Without this, the audit pipeline can silently break.
- [ ] Run audit script against the test vault; capture before/after byte + token counts for both tools.
- [ ] Write audit doc in the same format as `audit-2026-05-17-mcp-response-sizes-pr4.md`.
- [ ] Add CHANGELOG entries:
  - `list_concepts` now returns `{ concepts: Concept[], cursor?: string }` instead of bare `Concept[]`.
  - `list_domains` now returns `{ domains: Domain[], cursor?: string }` instead of bare `Domain[]`.
  - `list_domains` now applies a default limit of 100 (was unbounded).
- [ ] Commit.

---

## Test plan

| Test file | New tests | Dependencies |
|-----------|-----------|--------------|
| `list-concepts-tool.test.ts` (new) | ~21 | Tasks 6.1, 6.3, 6.4 |
| `list-domains-tool.test.ts` (new) | ~18 | Tasks 6.2, 6.3, 6.5 |
| `sqlite-reader.test.ts` or new `list-concepts-sql.test.ts` | ~4–6 (offset + tiebreaker) | Task 6.1 |
| (sqlite-reader for listDomains) | ~3–4 (limit + offset) | Task 6.2 |
| `audit-script.test.ts` (extend) | ~2 (new wrapper shapes — REGRESSION test) | Task 6.7 |

**Expected final test count:** 286 (baseline) + ~48–51 new = ~334–337 tests.

Run command for all new tests:
```bash
cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test -- --testPathPatterns="list-concepts|list-domains"
```

Full suite:
```bash
cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test
```

---

## Acceptance checklist

- [ ] `npm run build` — clean (no TypeScript errors).
- [ ] `npm test` — all tests green (target ~334–337 / 18 suites).
- [ ] `list_concepts` returns `{ concepts, cursor? }` (breaking change from bare array — CHANGELOG updated).
- [ ] `list_domains` returns `{ domains, cursor? }` (breaking change from bare array — CHANGELOG updated).
- [ ] `list_domains` default limit = 100 (unbounded was a footgun).
- [ ] `list_concepts` tiebreaker: `ORDER BY edgeCount DESC, c.slug ASC` — pagination stable across pages.
- [ ] `list_domains` tiebreaker: already deterministic (`slug` unique) — confirmed, no change.
- [ ] Identical-query refusal tested: `CURSOR_REQUIRED` on repeat call within 300s.
- [ ] Cursor binding tested: wrong-queryHash cursor → `CURSOR_INVALID_SIGNATURE` with "different query" message.
- [ ] Spec cursor-inputs table row 242 corrected: `q?, domain?` → `tags?, search?`.
- [ ] Audit doc created with before/after byte counts for both tools.
- [ ] CHANGELOG updated with both breaking-shape changes + list_domains default-limit change.
- [ ] No changes to `index.ts` (call sites propagate via `Parameters<>` typing).
