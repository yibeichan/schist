# MCP Context Efficiency — PR 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt cursor pagination and reason-string verbose in `search_memory` — the first real consumer of the PR 2 protocol modules. Default response carries a 200-code-point snippet of `content`; full content requires `verbose: "<reason ≥12 code points>"`. Identical queries within 300 s without a cursor are refused with `CURSOR_REQUIRED` (with the verbose-newly-set bypass). Bundles the Issue #60 fix (verbose ZWS-strip extension to ZWNJ/ZWJ/WORD JOINER) into the same PR since PR 3 is the first PR where a real consumer can be attacked via the bypass.

**Architecture:** Three layers, each adopted minimally. (1) SQL layer (`sqlite-reader.ts:searchMemory`) gains an `offset` parameter and explicit `ORDER BY` tiebreakers on both FTS and non-FTS paths — no other behavior change. (2) Pure-function snippet helper added under `mcp-server/src/protocol/snippet.ts` (codepoint-aware, ellipsis-suffixed, parameterized cap). (3) Tool handler (`tools.ts:search_memory`) becomes the protocol wiring point: `parseVerbose` → `canonicalizeQueryHash` → `decodeCursor`+binding-check OR `checkRefusal` → SQL → `recordIssued`+`issueCursor` if capped → `logVerbose`+`noteHighFrequency` if verbose → response shape `{ entries, cursor?, verboseNote? }`. The Issue #60 fix lives in `protocol/verbose.ts` strip regex.

**Tech Stack:** TypeScript (strict, ESM, Node ≥20). No new runtime dependencies. Jest 30 + ts-jest ESM preset; existing `mcp-server/jest.config.cjs` already configured. Existing `protocol/index.ts` already exports every primitive PR 3 needs.

**Tracks:** [yibeichan/schist#50](https://github.com/yibeichan/schist/issues/50) (PR 3 of 7-PR rollout) + [yibeichan/schist#60](https://github.com/yibeichan/schist/issues/60) (bundled). Spec contract: `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md`.

---

## Spec → task mapping

Every PR-3 row of the spec lands in exactly one task. PR 4–7 wiring is **out of scope** for PR 3.

| Spec section | PR 3 task |
|---|---|
| Cursor table row: `search_memory` (FTS path) `ORDER BY bm25(agent_memory_fts), m.id ASC` | Task 3.2 |
| Cursor table row: `search_memory` (non-FTS path) `ORDER BY created_at DESC, id ASC` | Task 3.2 |
| `queryHash` includes `query?, owner?, entry_type?, date_from?, date_to?, limit` + active owner | Task 3.5 |
| Default limits: limit 50, cap 200 | Task 3.5 (schema cap), Task 3.8 (server clamp) |
| Reason-string verbose adoption: `search_memory` full content requires verbose | Task 3.7 (handler), Task 3.3 (snippet helper) |
| Verbose-newly-set bypass against an actual verbose gate | Task 3.6 |
| Cursor binding to queryHash (spec amendment from PR 3) | Task 3.0 (spec doc) + Task 3.5 (impl) |
| Issue #60: ZWNJ/ZWJ/WJ strip in `parseVerbose` (bundled) | Task 3.1 |
| Audit re-measurement (search_memory ~42 KB → ~12 KB target) | Task 3.9 |

---

## Spec clarifications baked into PR 3

### Clarification A — cursor binding to queryHash (decided 2026-05-12)

The spec at `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md` lines 142–148 describes cursor consumption but is **silent on what happens when an agent presents a valid HMAC-signed cursor whose encoded `queryHash` does NOT equal the current call's computed `queryHash`** (i.e. cursor issued for query A, presented alongside args that compute query B's hash).

**Decision (locked):** the current call's computed `queryHash` MUST equal the cursor's encoded `queryHash`. On mismatch the tool returns `{ error: "CURSOR_INVALID_SIGNATURE", message: "cursor was issued for a different query — restart pagination from page 1" }`.

**Rationale:** the cursor's `queryHash` is part of the signed payload precisely so the tool can verify the cursor binds to *this* query, not some other query whose pagination state happens to be valid. Allowing pagination of query A's results inside a request envelope that says it's asking for query B creates three problems: (a) agents cannot reason about which result set they are paging through; (b) it silently turns a query-refinement attempt into a continuation of the prior query (the "cursor wins, args ignored" failure mode); (c) it widens the cursor's authority from "advance the same query" to "advance an arbitrary prior query," which weakens the structural property the protocol exists to enforce (an agent must consume a result page before getting another one for the *same* query).

**Alternatives considered:**

- **"Trust the cursor payload"** — the cursor's queryHash drives pagination; the current call's args are effectively decoration. Rejected: silently surprising behavior on accidental cursor-args drift, and structurally weaker than the chosen policy.
- **Introduce a fifth error code `CURSOR_QUERY_MISMATCH`** — distinct code, distinct agent action. Rejected: the agent action is identical to the existing `CURSOR_INVALID_SIGNATURE` path (drop cursor, restart from page 1), and `decodeCursor` itself can return signature errors for many root causes (forged HMAC, base64 corruption, payload schema). Folding query-mismatch into the same code keeps the agent-facing error surface minimal. The detail belongs in the `message` string, not in a new constant.

The spec is amended in Task 3.0 to encode this policy. Subsequent cursor PRs (4–7) MUST follow it.

### Clarification B — Issue #60 bundled (decided 2026-05-12)

[Issue #60](https://github.com/yibeichan/schist/issues/60) reports that the verbose reason-string gate in `protocol/verbose.ts` has a bypass: V8's `\s` regex class (even with `u` flag) does **NOT** match U+200C (ZWNJ), U+200D (ZWJ), or U+2060 (WORD JOINER). The current strip regex at `protocol/verbose.ts:52` strips only U+200B (ZWS) and U+FEFF (BOM). An agent can pass 12 invisible code points (e.g. `"‌".repeat(12)`) and satisfy the `≥12 code points after trim` gate, producing an audit-log line with no human-readable rationale.

**Decision (locked):** bundle the fix into PR 3. PR 3 is the first PR whose adoption surfaces real exposure (the verbose-newly-set bypass at the `search_memory` callsite). Fixing the strip in the same PR ships a secure verbose gate end-to-end.

**Scope of the fix (Task 3.1):**
- Extend the strip regex at `protocol/verbose.ts:52` to also cover U+200C, U+200D, U+2060.
- Add three regression tests (one per added code point) plus a "mixed bag" test confirming all five (U+200B, U+200C, U+200D, U+2060, U+FEFF) collapse together to an empty reason.
- Do **not** strip the wider `\p{Cf}` (format) category. That would also strip LRM (U+200E), RLM (U+200F), Arabic letter mark (U+061C), and bidi isolate controls — which are *semantically meaningful* in RTL strings. The targeted enumeration of {ZWS, ZWNJ, ZWJ, WJ, BOM} stays close to the issue's recommended fix.

The fix lands as a single commit inside PR 3 (Task 3.1).

---

## File structure

**Created in PR 3:**

- `mcp-server/src/protocol/snippet.ts` — `snippetContent(content: string, maxCodePoints?: number): string` pure helper.
- `mcp-server/tests/protocol/snippet.test.ts`
- `mcp-server/tests/search-memory-tool.test.ts` — integration tests over the full `tools.search_memory` protocol wiring.

**Modified in PR 3:**

- `mcp-server/src/protocol/verbose.ts` — strip regex line 52 extended (Issue #60 fix).
- `mcp-server/tests/protocol/verbose.test.ts` — add ZWNJ/ZWJ/WJ regression tests.
- `mcp-server/src/protocol/index.ts` — re-export `snippetContent` so PR 3+7 callsites consume a single barrel.
- `mcp-server/src/sqlite-reader.ts` — `searchMemory` adds optional `offset` param + ORDER BY tiebreakers (FTS + non-FTS). No other behavior change.
- `mcp-server/tests/memory.test.ts` — add tests for `offset` + tiebreaker determinism (does not affect existing assertions).
- `mcp-server/src/tools.ts` — `search_memory` handler becomes the protocol wiring point (parseVerbose → canonicalize → cursor decode/refuse → SQL → record/issue → log/freq → response).
- `mcp-server/src/tool-registry.ts` — `search_memory` inputSchema adds `cursor` (string) and `verbose` (string); description updated.
- `mcp-server/src/types.ts` — add `SearchMemoryResponse` type.
- `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md` — Clarification A (cursor binding) appended to the cursor-protocol section; the `search_memory` cursor table row already names the correct ORDER BY.

**Not touched in PR 3:**

- `protocol/cursor.ts` (no public-surface changes — PR 3 consumes the existing API).
- `tools.ts` handlers other than `search_memory` (PR 4–7 territory).

---

## Locked design decisions (consumed by every cursor PR going forward)

These extend the locked decisions in the umbrella plan and are re-affirmed here so PR 4–7 inherit them without re-deciding.

1. **Active owner derivation for cursor + LRU.** `activeOwner = process.env.SCHIST_AGENT_ID ?? ""`. Empty string is a valid owner namespace ("anonymous"). Mirrors the existing `assertAgentIdentity` pattern in `tools.ts:494`. `args.owner` is a FILTER inside the canonicalized args object — it is **not** the active owner.
2. **`limit` clamp.** Server-side clamp: `const effectiveLimit = Math.max(1, Math.min(args.limit ?? 50, 200))`. `limit:0` collapses to default 50 (matches `canonicalizeQueryHash` collapse rule). Cap 200 per spec "Default limits" table.
3. **`hasMore` detection.** Fetch `effectiveLimit + 1` rows; if returned ≥ effectiveLimit + 1, slice to effectiveLimit and set `hasMore = true`. No separate `COUNT(*)` query.
4. **Cursor issued only when `hasMore`.** No cursor in the response if all rows fit. `recordIssued` is called only when a cursor is issued.
5. **`recordIssued.verboseEnabled` = the verbose state of the call that **issued** the cursor.** This is what `checkRefusal` compares against on the next identical-query attempt. Already implemented by `protocol/cursor.ts:checkRefusal`.
6. **Cursor binding policy.** See Clarification A above. Implemented in Task 3.5.
7. **Cursor-payload `offset` is the source of truth.** When a cursor is presented and validates (HMAC + tool + TTL + queryHash binding), the SQL OFFSET is the cursor's `offset` field — *not* anything the caller passed. `args.offset` is not part of the public input schema and is silently ignored if present (canonicalization already strips unknown keys via JSON serialization of the canonical object — see `canonicalizeQueryHash` which serializes the WHOLE `args` object, so an extra `offset` arg DOES participate in the queryHash, but the SQL OFFSET comes from the cursor; this means a caller passing `{cursor, offset: 7}` will get a CURSOR_INVALID_SIGNATURE because the queryHash of `{cursor, offset:7}` differs from the queryHash of the original `{}` call. This is by design — the canonicalizer's "extra fields are part of identity" rule is what enforces it).
8. **Verbose state participates in response shape only.** Per spec line 145: "The `verbose` flag itself is **not** part of `queryHash`". `canonicalizeQueryHash` already excludes `verbose` by default. Do not override `opts.excludeKeys`.
9. **Snippet semantics.** 200 unicode code points (`[...content].slice(0, 200).join("")`) + `…` (U+2026) suffix iff the original exceeded 200 cp. Constant `SNIPPET_MAX_CODE_POINTS = 200` in `protocol/snippet.ts`.
10. **Response shape:** `{ entries: MemoryEntry[], cursor?: string, verboseNote?: string }`. No `truncated` flag (the presence of `cursor` is the truncation signal — matches the spec's "cursor as control flow" framing). No `hasMore` flag in the public response.

---

## Public API surface added in PR 3

```typescript
// mcp-server/src/protocol/snippet.ts

export const SNIPPET_MAX_CODE_POINTS = 200;

/**
 * Trims `content` to at most `maxCodePoints` Unicode code points (NOT UTF-16
 * code units). Appends "…" (U+2026) if truncated. Returns the input unchanged
 * if it already fits.
 *
 * Code-point semantics (`[...str].length`, not `str.length`) match the verbose
 * gate's counting rule, so a 200-CJK-character `content` returns 200 CJK chars
 * + ellipsis, not 100 chars cut mid-surrogate-pair.
 */
export function snippetContent(content: string, maxCodePoints?: number): string;
```

```typescript
// mcp-server/src/types.ts (added)

export interface SearchMemoryResponse {
  entries: MemoryEntry[];
  cursor?: string;
  verboseNote?: string;
}
```

```typescript
// mcp-server/src/sqlite-reader.ts (extended signature)

export function searchMemory(opts: {
  query?: string;
  owner?: string;
  entry_type?: string;
  date_from?: string;
  date_to?: string;
  limit?: number;
  offset?: number;             // NEW: default 0
}): MemoryEntry[];
// Returns rows respecting ORDER BY (FTS path: bm25 + m.id ASC; non-FTS path:
// created_at DESC + id ASC). Limit + offset go through directly. No snippet
// trimming (that's the tool layer).
```

No new exports from `protocol/cursor.ts` or `protocol/verbose.ts` (other than the Issue #60 fix being an internal regex change).

---

## Task 3.0: Spec amendment — cursor binding policy

**Files:**
- Modify: `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md` (append a "Cursor binding to queryHash" subsection under "Cursor protocol")

This is a docs-only commit. No tests. It precedes any code change so PR 3 reviewers can reference the amendment when judging Tasks 3.5+.

- [ ] **Step 1: Open the spec at the right anchor**

Locate the existing "Server-side identical-query refusal" subsection (around line 136). Insert the new subsection immediately AFTER the "Cursor error codes" table (around line 184, before "Multi-process cursor scope").

- [ ] **Step 2: Append the cursor-binding subsection**

Append the following markdown:

```markdown
### Cursor binding to queryHash

When an agent presents a cursor on a follow-up call, the tool computes the
current call's `queryHash` from the current args + active owner (excluding
`cursor` and `verbose` as per the canonicalization rule) and compares it
to the cursor's encoded `queryHash`. **Mismatch → `CURSOR_INVALID_SIGNATURE`**
with message `"cursor was issued for a different query — restart pagination
from page 1"`.

Rationale: the cursor's `queryHash` is part of the signed payload so the
tool can verify the cursor binds to *this* query, not some other query
whose pagination state happens to be valid. Allowing the cursor's payload
to override args silently turns a query-refinement attempt into a
continuation of the prior query — which weakens the structural property
the protocol exists to enforce.

Mismatch is folded into `CURSOR_INVALID_SIGNATURE` rather than a new error
code because the agent action is identical to the existing
`CURSOR_INVALID_SIGNATURE` path: drop the cursor, restart from page 1. The
specific root cause (queryHash mismatch vs HMAC mismatch vs payload-schema
mismatch) is communicated through the `message` string, not the error code
constant.

This policy is normative for PRs 3–7 (every cursor-adopting tool).
```

- [ ] **Step 3: Refresh the self-review checklist**

Add a new checklist bullet under "## Self-review checklist" (around line 405) after the existing `cursor error codes split` bullet (around line 430):

```markdown
- [x] Cursor binding to queryHash: current call's computed queryHash must
  equal cursor's encoded queryHash; mismatch returns `CURSOR_INVALID_SIGNATURE`
  with explanatory message.
```

- [ ] **Step 4: Update the PR-3 cross-reference**

The "PR 3" line of the self-review checklist already says `→ "search_memory" rows in cursor-adoption table + Default limits + Reason-string adopters + verbose-newly-set bypass.` Append `+ "Cursor binding to queryHash".`:

```markdown
  - PR 3 → "search_memory" rows in cursor-adoption table + Default
    limits + Reason-string adopters + verbose-newly-set bypass +
    "Cursor binding to queryHash".
```

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md
git commit -m "$(cat <<'EOF'
spec(#50): cursor binding to queryHash policy for PR 3+

Amends the cursor-protocol section with the rule that cursor payload's
queryHash must equal the current call's computed queryHash; mismatch
returns CURSOR_INVALID_SIGNATURE. Folded into the existing error code
rather than introducing CURSOR_QUERY_MISMATCH (agent action identical).
Normative for PRs 3-7.

Refs #50.

EOF
)"
```

---

## Task 3.1: Bundle Issue #60 fix — extend verbose strip regex

**Files:**
- Modify: `mcp-server/src/protocol/verbose.ts:52`
- Modify: `mcp-server/tests/protocol/verbose.test.ts` (add 4 regression tests)

The current strip strips only U+200B (ZWS) and U+FEFF (BOM). V8's `\s` matches NBSP+BOM but not ZWS/ZWNJ/ZWJ/WJ. The strip regex must be extended to cover U+200C (ZWNJ), U+200D (ZWJ), and U+2060 (WORD JOINER).

- [ ] **Step 1: Write the failing tests (Jest)**

Add these test cases to `mcp-server/tests/protocol/verbose.test.ts` inside the existing `describe("parseVerbose", () => { ... })` block:

```typescript
  it("strips U+200C (ZWNJ) so a 12-ZWNJ string parses as not-verbose (Issue #60)", () => {
    // 12 zero-width non-joiners — visible length 0, code-point count 12
    const input = "‌".repeat(12);
    const r = parseVerbose(input);
    expect(r).toEqual({ enabled: false });
  });

  it("strips U+200D (ZWJ) so a 12-ZWJ string parses as not-verbose (Issue #60)", () => {
    const input = "‍".repeat(12);
    const r = parseVerbose(input);
    expect(r).toEqual({ enabled: false });
  });

  it("strips U+2060 (WORD JOINER) so a 12-WJ string parses as not-verbose (Issue #60)", () => {
    const input = "⁠".repeat(12);
    const r = parseVerbose(input);
    expect(r).toEqual({ enabled: false });
  });

  it("strips the full {ZWS, ZWNJ, ZWJ, WJ, BOM} set in one mixed input (Issue #60)", () => {
    // 2 of each = 10 code points of invisibles + 2 NBSP = 12 cp total
    const input = "​​‌‌‍‍⁠⁠﻿﻿  ";
    const r = parseVerbose(input);
    expect(r).toEqual({ enabled: false });
  });

  it("does NOT strip U+200E (LRM) — bidi marks remain semantically meaningful (Issue #60 scope)", () => {
    // 12 LRMs SHOULD survive strip and the trim sees them as non-whitespace,
    // so a 12-LRM string is a valid (if useless) verbose reason. We test
    // explicitly to make the scope of the Issue #60 fix obvious in the suite.
    const input = "‎".repeat(12);
    const r = parseVerbose(input);
    expect(r).toEqual({ enabled: true, reason: input });
  });
```

- [ ] **Step 2: Run tests; verify the four "strips ..." tests fail**

Run: `cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test -- --testPathPatterns=protocol/verbose`

Expected: the four new "strips ..." tests fail (current strip regex misses ZWNJ/ZWJ/WJ; the mixed test reaches the `≥12 cp` gate and returns `{ enabled: true, reason: "..." }`). The LRM test passes (LRM was never in scope).

- [ ] **Step 3: Extend the strip regex**

Edit `mcp-server/src/protocol/verbose.ts` line 52:

```typescript
  // Whitespace-only or empty → not verbose, no error.
  // \s covers ASCII whitespace + NBSP (U+00A0). V8's \s does NOT match the
  // zero-width formatting characters {ZWS U+200B, ZWNJ U+200C, ZWJ U+200D,
  // WORD JOINER U+2060, BOM U+FEFF}, so we strip them explicitly before the
  // \s test. Bidi marks (LRM U+200E, RLM U+200F, ALM U+061C) are *not*
  // stripped — they carry semantic meaning in RTL strings.
  const stripped = input.replace(/[​‌‍⁠﻿]/gu, "");
  if (/^\s*$/u.test(stripped)) return { enabled: false };
```

- [ ] **Step 4: Run tests; verify all five new tests pass**

Run: `cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test -- --testPathPatterns=protocol/verbose`

Expected: all `parseVerbose` tests pass, including the four new strips and the LRM scope test.

- [ ] **Step 5: Commit**

```bash
git add mcp-server/src/protocol/verbose.ts mcp-server/tests/protocol/verbose.test.ts
git commit -m "$(cat <<'EOF'
fix(verbose): extend strip to ZWNJ/ZWJ/WORD JOINER (closes #60)

V8's \s class does not match U+200C (ZWNJ), U+200D (ZWJ), or U+2060
(WORD JOINER). A 12-code-point string of any of these would satisfy
the verbose reason-string >=12 cp gate while being entirely invisible,
producing an audit-log line with no human-readable rationale.

Targeted enumeration (5 chars: ZWS, ZWNJ, ZWJ, WJ, BOM) rather than
the wider \p{Cf} category — bidi marks (LRM/RLM/ALM) are semantically
meaningful in RTL text and must survive strip.

Bundled into PR 3 (first PR with a real verbose consumer at the
search_memory callsite).

Closes #60. Refs #50.

EOF
)"
```

---

## Task 3.2: SQL layer — `searchMemory` adds `offset` + ORDER BY tiebreakers

**Files:**
- Modify: `mcp-server/src/sqlite-reader.ts:394-447` (the `searchMemory` function)
- Modify: `mcp-server/tests/memory.test.ts` (extend the `describe("searchMemory", ...)` block)

Goal: extend `searchMemory` with an `offset?` param and deterministic ORDER BY in both paths. No other behavioral change. The function continues to return `MemoryEntry[]` (snippet trimming and pagination-state are handled at the tool layer).

- [ ] **Step 1: Write failing tests for offset + tiebreakers**

Add inside the existing `describe("searchMemory", () => { ... })` block in `mcp-server/tests/memory.test.ts`, AFTER the existing `beforeEach` that seeds three entries:

```typescript
  it("returns rows starting from the requested offset (non-FTS path)", () => {
    // Seed 5 entries with deterministic content (in addition to beforeEach's 3)
    for (let i = 0; i < 5; i++) {
      addMemoryAs("sansan", { entry_type: "decision", content: `pad-entry-${i}` });
    }
    const all = searchMemory({ owner: "sansan", limit: 50 });
    expect(all.length).toBeGreaterThanOrEqual(7); // 2 from beforeEach + 5
    const page1 = searchMemory({ owner: "sansan", limit: 3, offset: 0 });
    const page2 = searchMemory({ owner: "sansan", limit: 3, offset: 3 });
    expect(page1.length).toBe(3);
    expect(page2.length).toBeGreaterThanOrEqual(1);
    // Pages disjoint by id
    const ids1 = new Set(page1.map(r => r.id));
    for (const r of page2) {
      expect(ids1.has(r.id)).toBe(false);
    }
  });

  it("returns rows starting from the requested offset (FTS path)", () => {
    addMemoryAs("sansan", { entry_type: "decision", content: "tiebreaker fixture alpha" });
    addMemoryAs("sansan", { entry_type: "decision", content: "tiebreaker fixture beta" });
    addMemoryAs("sansan", { entry_type: "decision", content: "tiebreaker fixture gamma" });
    const page1 = searchMemory({ query: "tiebreaker fixture", limit: 2, offset: 0 });
    const page2 = searchMemory({ query: "tiebreaker fixture", limit: 2, offset: 2 });
    expect(page1.length).toBe(2);
    expect(page2.length).toBeGreaterThanOrEqual(1);
    const ids1 = new Set(page1.map(r => r.id));
    for (const r of page2) {
      expect(ids1.has(r.id)).toBe(false);
    }
  });

  it("non-FTS path orders by created_at DESC then id ASC (tiebreaker is deterministic)", () => {
    // Insert two entries with the same content; created_at granularity is 1s,
    // so two rapid inserts share the same created_at — id ASC must order them.
    addMemoryAs("sansan", { entry_type: "decision", content: "same-content-tiebreaker" });
    addMemoryAs("sansan", { entry_type: "decision", content: "same-content-tiebreaker" });
    const both = searchMemory({ owner: "sansan", entry_type: "decision" });
    const sameContentRows = both.filter(r => r.content === "same-content-tiebreaker");
    expect(sameContentRows.length).toBe(2);
    // When created_at ties, the LOWER id should appear earlier (id ASC tiebreaker
    // within the created_at DESC primary). The first-inserted has lower id and
    // appears earlier (because both share created_at — DESC has no effect on the
    // tie, so id ASC takes over).
    if (sameContentRows[0].created_at === sameContentRows[1].created_at) {
      expect(sameContentRows[0].id).toBeLessThan(sameContentRows[1].id);
    }
  });

  it("FTS path orders by bm25 then id ASC (tiebreaker is deterministic)", () => {
    // Two entries with identical bm25 rank against a generic query — id ASC
    // breaks ties.
    addMemoryAs("sansan", { entry_type: "decision", content: "identical relevance fixture" });
    addMemoryAs("sansan", { entry_type: "decision", content: "identical relevance fixture" });
    const rows = searchMemory({ query: "identical relevance fixture" });
    const matches = rows.filter(r => r.content === "identical relevance fixture");
    expect(matches.length).toBe(2);
    expect(matches[0].id).toBeLessThan(matches[1].id);
  });
```

- [ ] **Step 2: Run tests; verify failures**

Run: `cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test -- --testPathPatterns=memory.test`

Expected: the four new tests fail (no `offset` param recognised → ignored, no tiebreakers → ordering non-deterministic).

- [ ] **Step 3: Update searchMemory signature + body**

Edit `mcp-server/src/sqlite-reader.ts` (the `searchMemory` function spanning lines 394–447). Replace it with:

```typescript
export function searchMemory(opts: {
  query?: string;
  owner?: string;
  entry_type?: string;
  date_from?: string;
  date_to?: string;
  limit?: number;
  offset?: number;
}): MemoryEntry[] {
  const db = openMemoryDb();
  try {
    // Expire TTL-based agent_state rows while we have the DB open
    db.exec(`DELETE FROM agent_state WHERE ttl_hours IS NOT NULL AND
      datetime(updated_at, '+' || ttl_hours || ' hours') < datetime('now')`);

    const limit = opts.limit ?? 50;
    const offset = opts.offset ?? 0;
    const params: unknown[] = [];

    let useFts = false;
    if (opts.query) {
      useFts = true;
    }

    if (useFts) {
      let sql = `
        SELECT m.id, m.owner, m.date, m.entry_type, m.content, m.tags,
               m.related_doc, m.source_ref, m.confidence, m.created_at
        FROM agent_memory_fts f
        JOIN agent_memory m ON m.id = f.rowid
        WHERE agent_memory_fts MATCH ?
      `;
      params.push(sanitizeFtsQuery(opts.query!));
      if (opts.owner) { sql += " AND m.owner = ?"; params.push(opts.owner); }
      if (opts.entry_type) { sql += " AND m.entry_type = ?"; params.push(opts.entry_type); }
      if (opts.date_from) { sql += " AND m.date >= ?"; params.push(opts.date_from); }
      if (opts.date_to) { sql += " AND m.date <= ?"; params.push(opts.date_to); }
      sql += " ORDER BY bm25(agent_memory_fts), m.id ASC LIMIT ? OFFSET ?";
      params.push(limit, offset);
      const rows = db.prepare(sql).all(...params) as Record<string, unknown>[];
      return rows.map(rowToMemoryEntry);
    } else {
      let sql = "SELECT * FROM agent_memory WHERE 1=1";
      if (opts.owner) { sql += " AND owner = ?"; params.push(opts.owner); }
      if (opts.entry_type) { sql += " AND entry_type = ?"; params.push(opts.entry_type); }
      if (opts.date_from) { sql += " AND date >= ?"; params.push(opts.date_from); }
      if (opts.date_to) { sql += " AND date <= ?"; params.push(opts.date_to); }
      sql += " ORDER BY created_at DESC, id ASC LIMIT ? OFFSET ?";
      params.push(limit, offset);
      const rows = db.prepare(sql).all(...params) as Record<string, unknown>[];
      return rows.map(rowToMemoryEntry);
    }
  } finally {
    db.close();
  }
}
```

- [ ] **Step 4: Run tests; verify the four new tests pass and all existing pass**

Run: `cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test -- --testPathPatterns=memory.test`

Expected: all `searchMemory` tests pass, including the four new ones and all pre-existing.

- [ ] **Step 5: Commit**

```bash
git add mcp-server/src/sqlite-reader.ts mcp-server/tests/memory.test.ts
git commit -m "$(cat <<'EOF'
feat(sqlite-reader): searchMemory offset + ORDER BY tiebreakers (#50 PR 3)

Adds an `offset` parameter and deterministic ORDER BY to both FTS and
non-FTS paths:
  - FTS:     ORDER BY bm25(agent_memory_fts), m.id ASC
  - non-FTS: ORDER BY created_at DESC, id ASC

OFFSET-based pagination is non-deterministic without a secondary key
when the primary sort has duplicates. Adding `id ASC` is not a breaking
change to the primary sort. No snippet trimming yet — that lives at
the tool layer (Tasks 3.3, 3.7, 3.8).

Refs #50.

EOF
)"
```

---

## Task 3.3: Snippet helper — `snippetContent`

**Files:**
- Create: `mcp-server/src/protocol/snippet.ts`
- Create: `mcp-server/tests/protocol/snippet.test.ts`
- Modify: `mcp-server/src/protocol/index.ts` (add re-export)

Pure-function helper for trimming `content` to 200 code points with ellipsis. No state, no I/O.

- [ ] **Step 1: Write the failing tests**

Create `mcp-server/tests/protocol/snippet.test.ts`:

```typescript
import { describe, expect, it } from "@jest/globals";
import { snippetContent, SNIPPET_MAX_CODE_POINTS } from "../../src/protocol/snippet.js";

describe("SNIPPET_MAX_CODE_POINTS", () => {
  it("is 200 (spec default)", () => {
    expect(SNIPPET_MAX_CODE_POINTS).toBe(200);
  });
});

describe("snippetContent", () => {
  it("returns input unchanged if shorter than max", () => {
    expect(snippetContent("short content")).toBe("short content");
  });

  it("returns input unchanged if exactly at max (200 cp)", () => {
    const exactly200 = "a".repeat(200);
    expect(snippetContent(exactly200)).toBe(exactly200);
  });

  it("truncates to 200 code points + ellipsis when input exceeds max", () => {
    const input = "a".repeat(250);
    const out = snippetContent(input);
    expect([...out].length).toBe(201); // 200 + ellipsis
    expect(out).toBe("a".repeat(200) + "…");
  });

  it("counts code points (not UTF-16 units) for surrogate-pair-safe truncation", () => {
    // 250 emoji, each 2 UTF-16 units; str.length == 500, code points == 250
    const input = "\u{1F50D}".repeat(250); // U+1F50D = 🔍
    const out = snippetContent(input);
    expect([...out].length).toBe(201);
    // Must NOT split mid-surrogate-pair
    expect(out).toBe("\u{1F50D}".repeat(200) + "…");
  });

  it("handles CJK content correctly (1 cp == 1 BMP char)", () => {
    const input = "中".repeat(250); // 中
    const out = snippetContent(input);
    expect([...out].length).toBe(201);
    expect(out).toBe("中".repeat(200) + "…");
  });

  it("accepts a custom maxCodePoints", () => {
    expect(snippetContent("abcdefghij", 5)).toBe("abcde…");
    expect(snippetContent("abc", 5)).toBe("abc");
  });

  it("handles empty string", () => {
    expect(snippetContent("")).toBe("");
  });

  it("never appends ellipsis when input fits exactly", () => {
    // Edge: input of exactly N code points must NOT get ellipsis
    const exactly = "x".repeat(SNIPPET_MAX_CODE_POINTS);
    const out = snippetContent(exactly);
    expect(out).toBe(exactly);
    expect(out.endsWith("…")).toBe(false);
  });
});
```

- [ ] **Step 2: Run tests; verify failures**

Run: `cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test -- --testPathPatterns=protocol/snippet`

Expected: import error — `protocol/snippet.ts` does not exist yet.

- [ ] **Step 3: Implement snippet.ts**

Create `mcp-server/src/protocol/snippet.ts`:

```typescript
export const SNIPPET_MAX_CODE_POINTS = 200;
const ELLIPSIS = "…";

/**
 * Trims `content` to at most `maxCodePoints` Unicode code points (NOT UTF-16
 * code units), appending the ellipsis "…" iff truncation occurred. The
 * code-point spread (`[...str]`) is used because `str.slice(0, N)` slices
 * UTF-16 units and can split surrogate pairs mid-character.
 *
 * Returns the input unchanged when it already fits.
 */
export function snippetContent(content: string, maxCodePoints: number = SNIPPET_MAX_CODE_POINTS): string {
  const codePoints = [...content];
  if (codePoints.length <= maxCodePoints) return content;
  return codePoints.slice(0, maxCodePoints).join("") + ELLIPSIS;
}
```

- [ ] **Step 4: Re-export from protocol/index.ts**

Edit `mcp-server/src/protocol/index.ts` and append at the end of the file:

```typescript
export {
  SNIPPET_MAX_CODE_POINTS,
  snippetContent,
} from "./snippet.js";
```

- [ ] **Step 5: Run tests; verify all pass**

Run: `cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test -- --testPathPatterns=protocol/snippet`

Expected: all 8 tests pass.

- [ ] **Step 6: Commit**

```bash
git add mcp-server/src/protocol/snippet.ts mcp-server/src/protocol/index.ts mcp-server/tests/protocol/snippet.test.ts
git commit -m "$(cat <<'EOF'
feat(protocol): snippetContent helper (#50 PR 3)

Pure-function helper for code-point-aware content trimming, used by
search_memory (PR 3) and get_context full-mode (PR 7) to produce snippet
responses when verbose is not set.

  snippetContent(content, max=200) -> content | content[:max] + "…"

200 code points + ellipsis matches the spec's "first ~200 chars" default
snippet length, counted in Unicode code points (consistent with the
verbose gate's >=12 cp rule) so emoji and surrogate pairs are not split
mid-character.

Refs #50.

EOF
)"
```

---

## Task 3.4: Add `SearchMemoryResponse` type

**Files:**
- Modify: `mcp-server/src/types.ts`

Add the response interface. No tests — pure type addition is structurally validated by Task 3.5+.

- [ ] **Step 1: Append to types.ts**

Edit `mcp-server/src/types.ts`. After the existing `MemoryEntry` interface (around line 75), add:

```typescript
export interface SearchMemoryResponse {
  entries: MemoryEntry[];
  /** Opaque cursor token for the next page; absent when this is the last page. */
  cursor?: string;
  /** Soft warning when the verbose reason pattern has exceeded the rate limit. */
  verboseNote?: string;
}
```

- [ ] **Step 2: Verify typecheck still passes**

Run: `cd mcp-server && npm run build`

Expected: compiles cleanly. No new errors.

- [ ] **Step 3: No commit yet**

This change is staged but not committed — it's a type-only addition that needs Task 3.5's handler to actually use it. Bundle into the Task 3.5 commit.

---

## Task 3.5: `search_memory` handler — protocol wiring scaffold (parseVerbose + canonicalize)

**Files:**
- Modify: `mcp-server/src/tools.ts:503-512` (the `search_memory` function)
- Create: `mcp-server/tests/search-memory-tool.test.ts`

Replace the current direct passthrough with the first half of the protocol pipeline: parseVerbose → canonicalizeQueryHash. No cursor handling yet (Task 3.6); no SQL changes yet (Task 3.8). The function MUST type-check end-to-end at every step.

- [ ] **Step 1: Write the first batch of failing tests**

Create `mcp-server/tests/search-memory-tool.test.ts`:

```typescript
import { describe, expect, it, beforeEach, afterEach } from "@jest/globals";
import * as os from "os";
import * as path from "path";
import * as fs from "fs/promises";
import { search_memory } from "../src/tools.js";
import { addMemory } from "../src/sqlite-reader.js";
import { resetCursorForTesting, resetVerboseForTesting } from "../src/protocol/index.js";

let tempDir: string;
const VAULT_ROOT = "/tmp/not-used-by-memory-tools"; // search_memory ignores vaultRoot for memory db

beforeEach(async () => {
  tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-sm-tool-test-"));
  process.env.SCHIST_MEMORY_DB = path.join(tempDir, "test-memory.db");
  delete process.env.SCHIST_AGENT_ID;
  resetCursorForTesting();
  resetVerboseForTesting();
});

afterEach(async () => {
  delete process.env.SCHIST_MEMORY_DB;
  delete process.env.SCHIST_AGENT_ID;
  await fs.rm(tempDir, { recursive: true, force: true });
});

// Helper: seed N memory entries owned by `owner` so the tool has data to page
function seed(owner: string, n: number, contentPrefix = "entry"): void {
  const prev = process.env.SCHIST_AGENT_ID;
  process.env.SCHIST_AGENT_ID = owner;
  try {
    for (let i = 0; i < n; i++) {
      addMemory({ owner, entry_type: "decision", content: `${contentPrefix}-${i}` });
    }
  } finally {
    if (prev === undefined) delete process.env.SCHIST_AGENT_ID;
    else process.env.SCHIST_AGENT_ID = prev;
  }
}

describe("search_memory tool — verbose input parsing", () => {
  it("returns INVALID_ARG when verbose is a boolean", async () => {
    const r = await search_memory(VAULT_ROOT, { verbose: true } as never);
    expect(r).toEqual({ error: "INVALID_ARG", message: expect.stringContaining("verbose") });
  });

  it("returns INVALID_ARG when verbose is a too-short string", async () => {
    const r = await search_memory(VAULT_ROOT, { verbose: "short" } as never);
    expect(r).toEqual({ error: "INVALID_ARG", message: expect.stringContaining("code points") });
  });

  it("treats an empty verbose string as not-verbose (no error, no full content)", async () => {
    seed("sansan", 2);
    const r = await search_memory(VAULT_ROOT, { verbose: "" } as never);
    // Should be a valid response, NOT an error
    expect(r).toHaveProperty("entries");
  });

  it("treats omitted verbose as not-verbose", async () => {
    seed("sansan", 2);
    const r = await search_memory(VAULT_ROOT, {} as never);
    expect(r).toHaveProperty("entries");
  });
});

describe("search_memory tool — canonicalize errors", () => {
  it("returns INVALID_ARG when an arg is unhashable (NaN)", async () => {
    const r = await search_memory(VAULT_ROOT, { limit: NaN } as never);
    expect(r).toEqual({ error: "INVALID_ARG", message: expect.stringContaining("non-finite") });
  });
});
```

- [ ] **Step 2: Run tests; verify failures**

Run: `cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test -- --testPathPatterns=search-memory-tool`

Expected: every test fails (current `search_memory` ignores `verbose` entirely and never returns INVALID_ARG).

- [ ] **Step 3: Update tools.ts imports + scaffold the new handler**

Edit the top of `mcp-server/src/tools.ts` to import the protocol primitives. Find the existing imports near the top of the file and add:

```typescript
import {
  canonicalizeQueryHash,
  decodeCursor,
  issueCursor,
  recordIssued,
  checkRefusal,
  parseVerbose,
  logVerbose,
  noteHighFrequency,
  snippetContent,
} from "./protocol/index.js";
import type { SearchMemoryResponse } from "./types.js";
```

Then replace the existing `search_memory` function at lines 503–512 with the scaffold:

```typescript
export async function search_memory(
  _vaultRoot: string,
  args: {
    query?: string;
    owner?: string;
    entry_type?: string;
    date_from?: string;
    date_to?: string;
    limit?: number;
    cursor?: string;
    verbose?: string;
  }
): Promise<SearchMemoryResponse | { error: string; message: string }> {
  // Step 1: parseVerbose. Reject INVALID_ARG before any SQL or canonicalize work.
  const v = parseVerbose(args.verbose);
  if ("error" in v) return v.error;
  const verboseEnabled = v.enabled;
  // (Capture v.reason in a narrowed alias for downstream logging — Task 3.8)
  const verboseReason: string | undefined = v.enabled ? v.reason : undefined;

  // Step 2: canonicalizeQueryHash. Active owner is SCHIST_AGENT_ID or "".
  const activeOwner = process.env.SCHIST_AGENT_ID ?? "";
  const ch = canonicalizeQueryHash(args as Record<string, unknown>, activeOwner);
  if (!ch.ok) return ch.error;
  const queryHash = ch.queryHash;

  // Cursor + refusal + SQL + recordIssued + verbose log + response shape land
  // in Tasks 3.6, 3.7, 3.8. Until then return a minimal valid response so
  // Task 3.5 tests pass.
  void queryHash;
  void verboseEnabled;
  void verboseReason;
  try {
    const entries = sqliteReader.searchMemory(args);
    return { entries };
  } catch (e: unknown) {
    return normalizeError(e, "INVALID_SQL") as { error: string; message: string };
  }
}
```

- [ ] **Step 4: Run tests; verify all Task 3.5 tests pass**

Run: `cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test -- --testPathPatterns=search-memory-tool`

Expected: the 5 tests in the two describe blocks pass. Existing tests in `memory.test.ts` continue to pass (they exercise `searchMemory` directly, not the `search_memory` tool wrapper).

- [ ] **Step 5: Commit**

```bash
git add mcp-server/src/tools.ts mcp-server/src/types.ts mcp-server/tests/search-memory-tool.test.ts
git commit -m "$(cat <<'EOF'
feat(tools): search_memory parseVerbose + canonicalize scaffold (#50 PR 3)

Wires the first two protocol stages into the search_memory tool handler:

  args -> parseVerbose -> canonicalizeQueryHash -> (placeholder SQL)

INVALID_ARG envelopes are returned for:
  - verbose: boolean
  - verbose: string < 12 code points after trim
  - any unhashable arg (NaN, +/-Infinity, BigInt, circular ref, etc.)

Cursor handling, identical-query refusal, recordIssued, verbose logging,
and snippet response shape land in Tasks 3.6-3.8 below. Defines the
SearchMemoryResponse type for the eventual final shape.

Refs #50.

EOF
)"
```

---

## Task 3.6: `search_memory` handler — cursor decoding + queryHash binding check

**Files:**
- Modify: `mcp-server/src/tools.ts:search_memory` (replace the placeholder SQL block)
- Modify: `mcp-server/tests/search-memory-tool.test.ts` (add cursor-decode test cases)

Wire `decodeCursor` and enforce the queryHash binding policy from Task 3.0.

- [ ] **Step 1: Write the failing tests**

Add a new `describe` block to `mcp-server/tests/search-memory-tool.test.ts`:

```typescript
import { issueCursor } from "../src/protocol/index.js";

describe("search_memory tool — cursor decoding", () => {
  it("returns CURSOR_INVALID_SIGNATURE when the cursor signature is malformed", async () => {
    seed("sansan", 5);
    const r = await search_memory(VAULT_ROOT, { cursor: "garbage.notreallya.cursor" } as never);
    expect(r).toEqual({ error: "CURSOR_INVALID_SIGNATURE", message: expect.any(String) });
  });

  it("returns CURSOR_WRONG_TOOL when a cursor for a different tool is presented", async () => {
    seed("sansan", 5);
    // Forge a cursor for search_notes against our HMAC secret
    const c = issueCursor({ tool: "search_notes", queryHash: "deadbeef", offset: 5 });
    const r = await search_memory(VAULT_ROOT, { cursor: c } as never);
    expect(r).toEqual({ error: "CURSOR_WRONG_TOOL", message: expect.stringContaining("search_notes") });
  });

  it("returns CURSOR_INVALID_SIGNATURE when cursor queryHash does NOT match current args (binding policy)", async () => {
    seed("sansan", 5);
    // Issue a cursor with a deliberately wrong queryHash
    const c = issueCursor({ tool: "search_memory", queryHash: "0".repeat(64), offset: 2 });
    // Present that cursor on a search_memory call. The current args compute a
    // different queryHash, so binding must reject.
    const r = await search_memory(VAULT_ROOT, { cursor: c } as never);
    expect(r).toEqual({
      error: "CURSOR_INVALID_SIGNATURE",
      message: expect.stringContaining("different query"),
    });
  });

  it("accepts a cursor whose encoded queryHash matches the current args (round-trip)", async () => {
    seed("sansan", 10);
    // Compute the canonical queryHash for { owner: "sansan", limit: 3 } at activeOwner=""
    // We don't import canonicalize directly; instead we issue a cursor for the
    // result of a real call. Call once to capture the cursor that the tool
    // returns; on the next call with that cursor, pagination must advance.
    // (Full cursor issuance happens in Task 3.8; this test will need to be
    // re-validated as part of Task 3.8's integration tests. For now we only
    // assert "cursor with the correct queryHash does NOT error" — by issuing a
    // cursor matching the canonicalize result we mimic in tests.)
    //
    // Pragmatic approach: import canonicalizeQueryHash in this test and
    // generate the correct cursor.
    const { canonicalizeQueryHash } = await import("../src/protocol/index.js");
    const args = { owner: "sansan", limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    expect(ch.ok).toBe(true);
    if (!ch.ok) return;
    const c = issueCursor({ tool: "search_memory", queryHash: ch.queryHash, offset: 3 });
    const r = await search_memory(VAULT_ROOT, { ...args, cursor: c } as never);
    expect(r).toHaveProperty("entries");
    // The presence of `entries` and absence of an `error` field is sufficient
    // for Task 3.6. Full pagination assertions land in Task 3.8.
    expect(r).not.toHaveProperty("error");
  });
});
```

- [ ] **Step 2: Run tests; verify failures**

Run: `cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test -- --testPathPatterns=search-memory-tool`

Expected: the four new cursor-decode tests fail (current scaffold ignores `args.cursor`).

- [ ] **Step 3: Update the search_memory handler to decode cursors**

In `mcp-server/src/tools.ts`, edit the body of `search_memory` between the canonicalize step and the placeholder SQL block. Replace the `// Cursor + refusal + SQL ...` comment + placeholder body with:

```typescript
  // Step 3: Cursor decoding (if present) + queryHash binding check.
  let offset = 0;
  if (typeof args.cursor === "string" && args.cursor.length > 0) {
    const d = decodeCursor(args.cursor, "search_memory");
    if (!d.ok) return d.error;
    // Binding policy: cursor's queryHash must equal current call's queryHash.
    // See spec amendment in Task 3.0 ("Cursor binding to queryHash").
    if (d.queryHash !== queryHash) {
      return {
        error: "CURSOR_INVALID_SIGNATURE",
        message: "cursor was issued for a different query — restart pagination from page 1",
      };
    }
    offset = d.offset;
  }

  // Tasks 3.7 + 3.8 fill in: refusal check, SQL fetch with offset, response shape.
  void queryHash;
  void verboseEnabled;
  void verboseReason;
  void offset;
  try {
    const entries = sqliteReader.searchMemory({ ...args, offset });
    return { entries };
  } catch (e: unknown) {
    return normalizeError(e, "INVALID_SQL") as { error: string; message: string };
  }
```

- [ ] **Step 4: Run tests; verify all Task 3.6 tests pass + Task 3.5 still pass**

Run: `cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test -- --testPathPatterns=search-memory-tool`

Expected: 9 tests pass (5 from Task 3.5 + 4 from Task 3.6).

- [ ] **Step 5: Commit**

```bash
git add mcp-server/src/tools.ts mcp-server/tests/search-memory-tool.test.ts
git commit -m "$(cat <<'EOF'
feat(tools): search_memory cursor decode + binding check (#50 PR 3)

Wires decodeCursor + queryHash binding check into the handler:
  - HMAC mismatch          -> CURSOR_INVALID_SIGNATURE
  - tool mismatch          -> CURSOR_WRONG_TOOL
  - TTL expired            -> CURSOR_EXPIRED  (covered by protocol unit tests)
  - cursor for different
    query (binding policy) -> CURSOR_INVALID_SIGNATURE

Binding policy: current call's computed queryHash must equal the cursor's
encoded queryHash; mismatch is folded into CURSOR_INVALID_SIGNATURE with
an explanatory message. Spec amendment landed in the Task 3.0 commit.

Refs #50.

EOF
)"
```

---

## Task 3.7: `search_memory` handler — identical-query refusal + verbose-newly-set bypass

**Files:**
- Modify: `mcp-server/src/tools.ts:search_memory`
- Modify: `mcp-server/tests/search-memory-tool.test.ts`

Wire `checkRefusal` for the no-cursor path. The verbose-newly-set bypass is enforced by `checkRefusal` itself (per PR 2); PR 3 just needs to pass `verboseEnabled` correctly.

- [ ] **Step 1: Write the failing tests**

Add to `mcp-server/tests/search-memory-tool.test.ts`:

```typescript
describe("search_memory tool — identical-query refusal", () => {
  // Helper: drive the handler into "results were capped" state by issuing a
  // cursor manually. recordIssued lives in protocol/cursor.ts and is also
  // exposed via the barrel. We call it directly to simulate the state Task 3.8
  // will produce automatically.
  it("returns CURSOR_REQUIRED on identical (tool, queryHash, activeOwner) within TTL", async () => {
    seed("sansan", 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    const args = { owner: "sansan", limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "search_memory", queryHash: ch.queryHash, owner: "", verboseEnabled: false });
    // Second identical call (no cursor) must be refused
    const r = await search_memory(VAULT_ROOT, args as never);
    expect(r).toEqual({
      error: "CURSOR_REQUIRED",
      message: expect.stringContaining("Identical query"),
    });
  });

  it("does NOT refuse when activeOwner differs (different owner namespace)", async () => {
    seed("sansan", 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    const args = { owner: "sansan", limit: 3 };
    const ch = canonicalizeQueryHash(args, "yibei"); // recorded under yibei
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "search_memory", queryHash: ch.queryHash, owner: "yibei", verboseEnabled: false });
    // Current call's activeOwner is "" — different namespace, no refusal
    delete process.env.SCHIST_AGENT_ID;
    const r = await search_memory(VAULT_ROOT, args as never);
    expect(r).toHaveProperty("entries");
  });

  it("verbose-newly-set bypasses refusal (false -> true)", async () => {
    seed("sansan", 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    const args = { owner: "sansan", limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "search_memory", queryHash: ch.queryHash, owner: "", verboseEnabled: false });
    // Now retry the identical query with verbose newly set
    const r = await search_memory(VAULT_ROOT, {
      ...args,
      verbose: "user requested full content for review",
    } as never);
    expect(r).toHaveProperty("entries");
    expect(r).not.toHaveProperty("error");
  });

  it("STILL refuses on verbose true -> true (identical+verbose retry)", async () => {
    seed("sansan", 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    const args = { owner: "sansan", limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "search_memory", queryHash: ch.queryHash, owner: "", verboseEnabled: true });
    const r = await search_memory(VAULT_ROOT, {
      ...args,
      verbose: "user requested full content for review",
    } as never);
    expect(r).toEqual({
      error: "CURSOR_REQUIRED",
      message: expect.any(String),
    });
  });

  it("STILL refuses on verbose true -> false (downgrade)", async () => {
    seed("sansan", 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    const args = { owner: "sansan", limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "search_memory", queryHash: ch.queryHash, owner: "", verboseEnabled: true });
    const r = await search_memory(VAULT_ROOT, args as never);
    expect(r).toEqual({
      error: "CURSOR_REQUIRED",
      message: expect.any(String),
    });
  });
});
```

- [ ] **Step 2: Run tests; verify failures**

Run: `cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test -- --testPathPatterns=search-memory-tool`

Expected: the five new tests fail (current handler does not call `checkRefusal`).

- [ ] **Step 3: Wire checkRefusal into the no-cursor branch**

In `mcp-server/src/tools.ts`, edit the `search_memory` handler. Replace the existing cursor-handling block (from Task 3.6) with this expanded version:

```typescript
  // Step 3: Cursor decoding (if present) + queryHash binding check.
  let offset = 0;
  let consumingCursor = false;
  if (typeof args.cursor === "string" && args.cursor.length > 0) {
    const d = decodeCursor(args.cursor, "search_memory");
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

  // Step 4: Identical-query refusal (only when no cursor was presented).
  if (!consumingCursor) {
    const refusal = checkRefusal({
      tool: "search_memory",
      queryHash,
      owner: activeOwner,
      verboseEnabled,
    });
    if (refusal.refuse) return refusal.error;
  }

  // Tasks 3.8: SQL fetch with offset + recordIssued + verbose log + response shape
  void verboseReason;
  try {
    const entries = sqliteReader.searchMemory({ ...args, offset });
    return { entries };
  } catch (e: unknown) {
    return normalizeError(e, "INVALID_SQL") as { error: string; message: string };
  }
```

- [ ] **Step 4: Run tests; verify all Task 3.7 tests pass + 3.5/3.6 still pass**

Run: `cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test -- --testPathPatterns=search-memory-tool`

Expected: 14 tests pass (5 from 3.5 + 4 from 3.6 + 5 from 3.7).

- [ ] **Step 5: Commit**

```bash
git add mcp-server/src/tools.ts mcp-server/tests/search-memory-tool.test.ts
git commit -m "$(cat <<'EOF'
feat(tools): search_memory identical-query refusal (#50 PR 3)

Wires checkRefusal into the no-cursor branch of search_memory. The
verbose-newly-set bypass is enforced by checkRefusal itself (PR 2);
PR 3 just needs to pass verboseEnabled correctly.

Test matrix locked here:
  - identical query (no cursor)                -> CURSOR_REQUIRED
  - different activeOwner                       -> not refused
  - verbose newly set (false -> true)           -> bypass
  - verbose true -> true (identical+verbose)    -> STILL refused
  - verbose downgrade (true -> false)           -> STILL refused

Refs #50.

EOF
)"
```

---

## Task 3.8: `search_memory` handler — SQL fetch + cursor issuance + verbose log + response shape

**Files:**
- Modify: `mcp-server/src/tools.ts:search_memory`
- Modify: `mcp-server/tests/search-memory-tool.test.ts`

The final wiring: fetch `limit + 1` rows to detect `hasMore`, snippet-trim content when verbose is off, issue cursor + recordIssued if results were capped, log + freq-track if verbose is on, return `SearchMemoryResponse`.

- [ ] **Step 1: Write the failing tests**

Add to `mcp-server/tests/search-memory-tool.test.ts`:

```typescript
describe("search_memory tool — snippet vs full content", () => {
  it("returns 200-cp snippet by default (verbose off)", async () => {
    const long = "x".repeat(500);
    const prev = process.env.SCHIST_AGENT_ID;
    process.env.SCHIST_AGENT_ID = "sansan";
    addMemory({ owner: "sansan", entry_type: "lesson", content: long });
    if (prev === undefined) delete process.env.SCHIST_AGENT_ID;
    else process.env.SCHIST_AGENT_ID = prev;
    const r = await search_memory(VAULT_ROOT, { owner: "sansan", limit: 50 } as never);
    expect(r).toHaveProperty("entries");
    if (!("entries" in r)) throw new Error("expected entries");
    expect(r.entries[0].content.length).toBeLessThanOrEqual(201); // 200 + ellipsis
    expect(r.entries[0].content.endsWith("…")).toBe(true);
  });

  it("returns full content when verbose is set with a valid reason", async () => {
    const long = "x".repeat(500);
    const prev = process.env.SCHIST_AGENT_ID;
    process.env.SCHIST_AGENT_ID = "sansan";
    addMemory({ owner: "sansan", entry_type: "lesson", content: long });
    if (prev === undefined) delete process.env.SCHIST_AGENT_ID;
    else process.env.SCHIST_AGENT_ID = prev;
    const r = await search_memory(VAULT_ROOT, {
      owner: "sansan",
      limit: 50,
      verbose: "manually inspecting full lesson content",
    } as never);
    expect(r).toHaveProperty("entries");
    if (!("entries" in r)) throw new Error("expected entries");
    expect(r.entries[0].content).toBe(long);
  });

  it("does NOT append ellipsis when content fits within 200 cp", async () => {
    const short = "this is a short lesson";
    const prev = process.env.SCHIST_AGENT_ID;
    process.env.SCHIST_AGENT_ID = "sansan";
    addMemory({ owner: "sansan", entry_type: "lesson", content: short });
    if (prev === undefined) delete process.env.SCHIST_AGENT_ID;
    else process.env.SCHIST_AGENT_ID = prev;
    const r = await search_memory(VAULT_ROOT, { owner: "sansan", limit: 50 } as never);
    if (!("entries" in r)) throw new Error("expected entries");
    expect(r.entries[0].content).toBe(short);
    expect(r.entries[0].content.endsWith("…")).toBe(false);
  });
});

describe("search_memory tool — pagination + cursor issuance", () => {
  it("returns a cursor when results are capped and rows.length === limit", async () => {
    seed("sansan", 10);
    const r = await search_memory(VAULT_ROOT, { owner: "sansan", limit: 3 } as never);
    if (!("entries" in r)) throw new Error("expected entries");
    expect(r.entries.length).toBe(3);
    expect(typeof r.cursor).toBe("string");
  });

  it("does NOT return a cursor when results fit (rows.length < limit)", async () => {
    seed("sansan", 2);
    const r = await search_memory(VAULT_ROOT, { owner: "sansan", limit: 50 } as never);
    if (!("entries" in r)) throw new Error("expected entries");
    expect(r.entries.length).toBe(2);
    expect(r.cursor).toBeUndefined();
  });

  it("cursor advances pagination — page 1 + cursor → page 2", async () => {
    seed("sansan", 10);
    const r1 = await search_memory(VAULT_ROOT, { owner: "sansan", limit: 3 } as never);
    if (!("entries" in r1)) throw new Error("expected entries");
    expect(r1.cursor).toBeDefined();
    const r2 = await search_memory(VAULT_ROOT, {
      owner: "sansan",
      limit: 3,
      cursor: r1.cursor,
    } as never);
    if (!("entries" in r2)) throw new Error("expected entries");
    expect(r2.entries.length).toBe(3);
    const ids1 = new Set(r1.entries.map(e => e.id));
    for (const e of r2.entries) {
      expect(ids1.has(e.id)).toBe(false);
    }
  });

  it("the last page does NOT return a cursor", async () => {
    seed("sansan", 5);
    // page-1 has 3 rows + cursor; page-2 has 2 rows + no cursor
    const r1 = await search_memory(VAULT_ROOT, { owner: "sansan", limit: 3 } as never);
    if (!("entries" in r1) || !r1.cursor) throw new Error("expected page-1 cursor");
    const r2 = await search_memory(VAULT_ROOT, {
      owner: "sansan",
      limit: 3,
      cursor: r1.cursor,
    } as never);
    if (!("entries" in r2)) throw new Error("expected entries");
    expect(r2.entries.length).toBe(2);
    expect(r2.cursor).toBeUndefined();
  });

  it("clamps limit at 200 (cap from spec)", async () => {
    seed("sansan", 50);
    const r = await search_memory(VAULT_ROOT, { owner: "sansan", limit: 9999 } as never);
    if (!("entries" in r)) throw new Error("expected entries");
    expect(r.entries.length).toBeLessThanOrEqual(200);
  });

  it("collapses limit: 0 to default 50", async () => {
    seed("sansan", 60);
    const r = await search_memory(VAULT_ROOT, { owner: "sansan", limit: 0 } as never);
    if (!("entries" in r)) throw new Error("expected entries");
    // limit collapsed to default 50, so we get 50 entries + a cursor
    expect(r.entries.length).toBe(50);
    expect(r.cursor).toBeDefined();
  });
});

describe("search_memory tool — verbose logging + frequency tracker", () => {
  it("emits a verboseNote when the same reason exceeds 30 hits in 60 s", async () => {
    seed("sansan", 5);
    const reason = "manually inspecting full lesson content";
    // Call 31 times — the 31st must yield verboseNote
    let last: unknown;
    for (let i = 0; i < 31; i++) {
      // Each call uses fresh refusal state (different queryHash via altered limit)
      // to avoid identical-query refusal. We vary `query` to keep queryHash distinct.
      last = await search_memory(VAULT_ROOT, {
        owner: "sansan",
        limit: 50,
        query: `vary-${i}`,
        verbose: reason,
      } as never);
    }
    expect(last).toHaveProperty("entries");
    if (last && typeof last === "object" && "verboseNote" in last) {
      expect(last.verboseNote).toMatch(/frequent/);
    } else {
      throw new Error(`expected verboseNote on the 31st call, got ${JSON.stringify(last)}`);
    }
  });

  it("writes a [verbose] audit line to stderr when verbose is enabled", async () => {
    seed("sansan", 2);
    const spy = jest.spyOn(process.stderr, "write").mockImplementation(() => true);
    try {
      await search_memory(VAULT_ROOT, {
        owner: "sansan",
        limit: 50,
        verbose: "auditing this memory query for completeness",
      } as never);
      const calls = spy.mock.calls.map(c => String(c[0]));
      const verboseLines = calls.filter(s => s.startsWith("[verbose] search_memory"));
      expect(verboseLines.length).toBe(1);
    } finally {
      spy.mockRestore();
    }
  });
});
```

Note: `jest` is the global jest object from `@jest/globals`. Add the import to the top of the test file: `import { jest } from "@jest/globals";`.

- [ ] **Step 2: Run tests; verify failures**

Run: `cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test -- --testPathPatterns=search-memory-tool`

Expected: the new tests fail (placeholder body does not snippet, paginate, recordIssued, log, or freq-track).

- [ ] **Step 3: Final search_memory handler body**

Replace the entire `search_memory` function in `mcp-server/src/tools.ts` with the final version:

```typescript
export async function search_memory(
  _vaultRoot: string,
  args: {
    query?: string;
    owner?: string;
    entry_type?: string;
    date_from?: string;
    date_to?: string;
    limit?: number;
    cursor?: string;
    verbose?: string;
  }
): Promise<SearchMemoryResponse | { error: string; message: string }> {
  // Step 1: parseVerbose
  const v = parseVerbose(args.verbose);
  if ("error" in v) return v.error;
  const verboseEnabled = v.enabled;
  const verboseReason: string | undefined = v.enabled ? v.reason : undefined;

  // Step 2: canonicalizeQueryHash. Active owner is SCHIST_AGENT_ID or "".
  const activeOwner = process.env.SCHIST_AGENT_ID ?? "";
  const ch = canonicalizeQueryHash(args as Record<string, unknown>, activeOwner);
  if (!ch.ok) return ch.error;
  const queryHash = ch.queryHash;

  // Step 3: Cursor decoding + queryHash binding check.
  let offset = 0;
  let consumingCursor = false;
  if (typeof args.cursor === "string" && args.cursor.length > 0) {
    const d = decodeCursor(args.cursor, "search_memory");
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

  // Step 4: Identical-query refusal (only when no cursor was presented).
  if (!consumingCursor) {
    const refusal = checkRefusal({
      tool: "search_memory",
      queryHash,
      owner: activeOwner,
      verboseEnabled,
    });
    if (refusal.refuse) return refusal.error;
  }

  // Step 5: SQL fetch with limit + 1 to detect hasMore.
  // Server-side limit clamp + zero-collapse (mirrors canonicalize collapse rule)
  const requested = args.limit;
  const effectiveLimit = (() => {
    if (requested === undefined || requested === null || requested === 0) return 50;
    return Math.max(1, Math.min(requested, 200));
  })();

  let rows: import("./types.js").MemoryEntry[];
  try {
    rows = sqliteReader.searchMemory({
      query: args.query,
      owner: args.owner,
      entry_type: args.entry_type,
      date_from: args.date_from,
      date_to: args.date_to,
      limit: effectiveLimit + 1,
      offset,
    });
  } catch (e: unknown) {
    return normalizeError(e, "INVALID_SQL") as { error: string; message: string };
  }

  const hasMore = rows.length > effectiveLimit;
  const pageRows = hasMore ? rows.slice(0, effectiveLimit) : rows;

  // Step 6: Snippet vs full content
  const entries = verboseEnabled
    ? pageRows
    : pageRows.map(r => ({ ...r, content: snippetContent(r.content) }));

  // Step 7: Cursor issuance + recordIssued (only when results were capped)
  let cursor: string | undefined;
  if (hasMore) {
    recordIssued({
      tool: "search_memory",
      queryHash,
      owner: activeOwner,
      verboseEnabled,
    });
    cursor = issueCursor({
      tool: "search_memory",
      queryHash,
      offset: offset + effectiveLimit,
    });
  }

  // Step 8: Verbose audit log + frequency tracker
  let verboseNote: string | undefined;
  if (verboseEnabled && verboseReason !== undefined) {
    logVerbose({ tool: "search_memory", owner: activeOwner, reason: verboseReason });
    const note = noteHighFrequency({
      tool: "search_memory",
      owner: activeOwner,
      reason: verboseReason,
    });
    if (note !== null) verboseNote = note;
  }

  const response: SearchMemoryResponse = { entries };
  if (cursor !== undefined) response.cursor = cursor;
  if (verboseNote !== undefined) response.verboseNote = verboseNote;
  return response;
}
```

- [ ] **Step 4: Run tests; verify all Task 3.8 tests pass + all prior tests pass**

Run: `cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test -- --testPathPatterns="search-memory-tool|memory.test|protocol"`

Expected: all `search_memory` tool tests pass (5 from 3.5 + 4 from 3.6 + 5 from 3.7 + 10 from 3.8 = 24). `memory.test.ts` continues to pass (10+ existing + 4 from Task 3.2 = 14+). `protocol/cursor.test.ts`, `protocol/verbose.test.ts`, `protocol/snippet.test.ts` all pass.

- [ ] **Step 5: Commit**

```bash
git add mcp-server/src/tools.ts mcp-server/tests/search-memory-tool.test.ts
git commit -m "$(cat <<'EOF'
feat(tools): search_memory snippet + cursor issuance + verbose log (#50 PR 3)

Final wiring of the search_memory handler protocol pipeline:

  parseVerbose -> canonicalizeQueryHash -> decodeCursor(+binding) OR
  checkRefusal -> SQL(limit+1, offset) -> snippet vs full content ->
  recordIssued + issueCursor (if capped) -> logVerbose +
  noteHighFrequency (if verbose) -> { entries, cursor?, verboseNote? }

Server-side limit clamp: max 200 (spec cap), zero collapses to default 50
(matches canonicalize collapse). Fetch limit+1 to detect hasMore without
a separate COUNT query. Snippet uses snippetContent(200 cp + ellipsis).

Refs #50.

EOF
)"
```

---

## Task 3.9: Tool-registry schema + dispatch wiring

**Files:**
- Modify: `mcp-server/src/tool-registry.ts:36-51` (the `search_memory` entry)
- No changes to `mcp-server/src/index.ts` (the existing `case "search_memory"` dispatch is unchanged — the return type widened from `unknown` to `SearchMemoryResponse | { error }`, both serialize via `JSON.stringify`).

- [ ] **Step 1: Update the inputSchema and description**

Edit `mcp-server/src/tool-registry.ts`. Replace the `search_memory` block (lines 36–51) with:

```typescript
    {
      name: "search_memory",
      description: "Search agent memory entries by text, owner, type, or date range. Returns content snippets (200 code points) by default; pass verbose: \"<reason >=12 chars>\" to get full content. Paginated: when results are capped, the response includes a `cursor` token — echo it back on the next call to advance, or refine the query. Identical queries within 300s without a cursor are refused with CURSOR_REQUIRED.",
      inputSchema: {
        type: "object" as const,
        properties: {
          query: { type: "string" },
          owner: { type: "string" },
          entry_type: { type: "string", enum: ["decision", "lesson", "blocker", "completion", "observation"] },
          date_from: { type: "string" },
          date_to: { type: "string" },
          limit: { type: "number", description: "Default 50, capped at 200." },
          cursor: { type: "string", description: "Opaque pagination cursor returned by a prior call. Echo verbatim; do not modify." },
          verbose: { type: "string", description: "Reason (>=12 Unicode code points after trim) gating full-content return. Logged to server stderr for audit." },
        },
      },
    },
```

- [ ] **Step 2: Verify typecheck still passes and full test suite green**

Run: `cd mcp-server && npm run build && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test`

Expected: clean build; entire test suite green (existing 168+ tests from PR 2 + Task 3.1–3.8 additions).

- [ ] **Step 3: Commit**

```bash
git add mcp-server/src/tool-registry.ts
git commit -m "$(cat <<'EOF'
feat(tool-registry): search_memory schema for cursor + verbose (#50 PR 3)

Updates inputSchema to document the cursor and verbose fields agents
can now pass. Description rewritten to call out:
  - default snippet (200 cp) vs verbose full content
  - cursor echo-back convention
  - identical-query refusal within 300s TTL

Refs #50.

EOF
)"
```

---

## Task 3.10: Audit re-measurement + spec coverage walkthrough

**Files:**
- No code changes. Re-runs the audit script from PR 2 against a fixture vault populated with realistic memory entries.

The spec promised "42 KB / ~10.6K tokens baseline drops to ~12 KB / ~3K tokens estimated under snippet mode." This task verifies the actual measurement is in that ballpark.

- [ ] **Step 1: Re-run the audit script**

The audit script is at `scripts/audit_mcp_response_sizes.ts` (added in PR 1, modified in PR 2's run). Re-run against an HPC vault populated with memory entries:

```bash
cd /orcd/home/002/yibei/schist
SCHIST_MEMORY_DB="$HOME/.openclaw/memory/agent-state.db" \
  npx tsx scripts/audit_mcp_response_sizes.ts \
  --vault ~/schist-vault \
  --output docs/superpowers/specs/audit-2026-05-14-mcp-response-sizes-pr3.md
```

Expected: a markdown table showing per-tool byte and token counts. `search_memory` should drop from the ~42 KB baseline to ~10–15 KB (default snippet mode, no verbose).

- [ ] **Step 2: Read the audit output**

Open `docs/superpowers/specs/audit-2026-05-14-mcp-response-sizes-pr3.md` and verify:
- `search_memory` byte count: target ≤ 15 KB (vs baseline ~42 KB)
- Token count: target ≤ 4K tokens (vs baseline ~10.6K)
- `search_memory(verbose=...)` byte count: comparable to or slightly higher than baseline (full content returned)

If the search_memory numbers are NOT in the expected ranges, investigate:
- Is the fixture vault's memory DB actually populated? (Many local devs have empty/sparse memory DBs.)
- Are the entries long enough to actually exercise snippet trim? (If entries are all <200 cp, snippet mode is a no-op.)

- [ ] **Step 3: Run the spec coverage walkthrough**

Open `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md` and confirm:

- "Tool-specific cursor adoption" table, row `search_memory` (FTS path): `ORDER BY bm25(agent_memory_fts), m.id ASC` — implemented in Task 3.2 ✓
- "Tool-specific cursor adoption" table, row `search_memory` (non-FTS path): `ORDER BY created_at DESC, id ASC` — implemented in Task 3.2 ✓
- "Default limits" table, row `search_memory`: 50 default, 200 cap, snippet default — implemented in Tasks 3.5/3.8/3.9 ✓
- "Reason-string verbose" → "Tools adopting reason-string verbose": `search_memory` — implemented in Task 3.8 ✓
- "Server-side identical-query refusal" → verbose-newly-set bypass — implemented in Task 3.7 (delegated to `checkRefusal`) ✓
- "Cursor binding to queryHash" (spec amendment from Task 3.0) — implemented in Task 3.6 ✓
- Issue #60 fix — implemented in Task 3.1 ✓

- [ ] **Step 4: Commit the audit output**

```bash
git add docs/superpowers/specs/audit-2026-05-14-mcp-response-sizes-pr3.md
git commit -m "$(cat <<'EOF'
docs(audit): search_memory PR 3 response-size measurements (#50)

Re-runs the audit harness post-PR-3 to verify search_memory's response
shape change (full content -> 200-cp snippet by default) drops the
per-call byte/token budget. Verbose-mode call included as a control.

Refs #50.

EOF
)"
```

---

## PR description (final commit message + body)

When opening the PR via `gh pr create`, use this title and body:

**Title:** `feat(mcp-server): search_memory cursor + verbose adoption (#50 PR 3) + #60 fix`

**Body:**

```markdown
First real consumer of the PR 2 cursor + verbose protocol modules. Adopts
the full pipeline on `search_memory`:

- **Default snippet response** — content trimmed to 200 Unicode code points
  + `…` ellipsis when truncated. Full content requires
  `verbose: "<reason ≥12 code points>"`.
- **Cursor pagination** — server fetches `limit + 1` rows; if capped,
  response includes an HMAC-signed cursor. Agent echoes it back to
  paginate. Default limit 50, capped at 200.
- **Identical-query refusal** — same `(tool, queryHash, activeOwner)`
  within 300 s TTL without a cursor returns `CURSOR_REQUIRED`. The
  verbose-newly-set bypass (false → true) lets agents upgrade
  snippet → full content without a refusal loop. true → true and
  true → false (downgrade) remain refused.
- **Cursor binding** — current call's computed `queryHash` must equal the
  cursor's encoded `queryHash`; mismatch returns
  `CURSOR_INVALID_SIGNATURE` with message
  "cursor was issued for a different query — restart pagination". Policy
  amended into the spec (Task 3.0); normative for PRs 4–7.
- **Verbose audit log + rate-limit note** — every verbose call writes a
  `[verbose] search_memory by <owner>: "<reason>"` line to stderr;
  `(tool, owner, sha256(reason))` exceeding 30 hits/min adds a
  `verboseNote` to the response.
- **Issue #60 bundled** — verbose strip regex extended to U+200C (ZWNJ),
  U+200D (ZWJ), U+2060 (WORD JOINER) so a 12-invisible-char "reason"
  no longer passes the gate. Bidi marks (LRM/RLM/ALM) intentionally
  preserved.

**Spec amendment included:** "Cursor binding to queryHash" subsection
appended to `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md`
(Task 3.0 commit). This policy is normative for PRs 4–7.

**No new runtime dependencies.** No changes to other tools' behavior.
Audit re-measurement in
`docs/superpowers/specs/audit-2026-05-14-mcp-response-sizes-pr3.md`.

Closes #60.
Refs #50.
```

---

## Self-review checklist (run after every task lands)

This mirrors PR 2's checklist. Walk through it before pushing.

- [ ] Every requirement in the spec → task mapping table has a passing test.
- [ ] No `TBD` / `<fill>` placeholders in the plan.
- [ ] All non-ASCII test literals use `\uNNNN` escapes (lesson from PR 2:
  markdown rendering silently mangles literal NBSP/ZWS/ZWJ/CJK).
- [ ] Snippet semantics agree with verbose code-point semantics (both use
  `[...str].length`, not `str.length`).
- [ ] Discriminated-union narrowing uses `"error" in v` (PR 2 ParseVerboseResult
  lesson — `enabled: false` shared by silent-off and error variants).
- [ ] Cursor binding policy locked in spec amendment (Task 3.0) **before**
  the implementation lands (Task 3.6).
- [ ] Issue #60 fix limited to {ZWS, ZWNJ, ZWJ, WJ, BOM} — bidi marks
  (LRM/RLM/ALM) explicitly preserved with a regression test.
- [ ] Verbose-newly-set bypass test matrix matches PR 2's protocol test
  expectations (false→true bypass; true→true and true→false refused).
- [ ] `recordIssued.verboseEnabled` reflects the call that issued the cursor
  (not the call that will redeem it).
- [ ] LRU + frequency-bucket caps are per-module-state and survive across
  tests via `resetCursorForTesting` / `resetVerboseForTesting`.
- [ ] Server-side limit clamp + zero-collapse matches the canonicalize
  collapse rule (so the queryHash on `limit: 0` equals the queryHash on
  omitted `limit`).
- [ ] Audit script re-run confirms the response-size drop is in the
  expected range (~12 KB target, ~42 KB baseline).
- [ ] PR description names the spec amendment and the bundled Issue #60
  fix explicitly.

---

## Hold gate before pushing the PR

After all 10 tasks land locally and `npm test` is green:

1. **Re-validate the spec self-review checklist** in `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md`. The Task 3.0 commit added a new "Cursor binding to queryHash" bullet — confirm it reads cleanly and references the right error code.
2. **Run `npm run build` from `mcp-server/`** — type errors must be zero.
3. **Run the full test suite** with HPC LD_LIBRARY_PATH override:
   `LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test`
4. **Run `/review` on the diff** before opening the PR. Use `/review --base main` from the branch. Expect 0–3 minor findings; defense-in-depth observations should be evaluated like the PR 2 amendments.
5. **Push the branch and open the PR** with the description above. Tag the PR body with `Closes #60.` and `Refs #50.` so GitHub auto-links.

---

## Out-of-scope for PR 3 (carries forward)

- `query_graph` adoption (PR 4) — subquery wrap + caller-SQL passthrough.
- `search_notes` adoption (PR 5) — snippet already exists; just cursor.
- `list_concepts` + `list_domains` adoption (PR 6) — small tools.
- `get_context` `depth: "full"` reason-string gate (PR 7) — second verbose
  consumer.
- `tool-registry` description updates across all tools (PR 8) — migration.

PR 3 establishes the working pattern that PRs 4–7 mirror — most of the
copy-paste work is the handler scaffold from Tasks 3.5–3.8. Issue #60 is
fully fixed here so PR 7 inherits the secure verbose gate.
