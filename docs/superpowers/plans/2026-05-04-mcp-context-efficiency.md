# MCP Context Efficiency — Multi-PR Rollout Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce agent-context bloat from schist MCP tool responses by tightening default return shapes, adding cursor-based pagination, and replacing sticky verbose flags with reason-string opt-ins.

**Architecture:** Audit-then-spec-then-multi-PR rollout. PR 1 lands measurements + design; PRs 2–8 implement tool family by tool family, all referencing the spec. New shared utilities (cursor module, reason-string verbose) live in `mcp-server/src/protocol/` so individual tools depend on one well-tested module.

**Tech Stack:** TypeScript (Node ≥20), `better-sqlite3`, Jest 30 (ESM mode), Python 3.12+ (audit script), markdown for spec.

**Tracks:** [yibeichan/schist#50](https://github.com/yibeichan/schist/issues/50). Refinements adopted from m13v's comment.

---

## Guiding Principles

The lesson from m13v (saved as schist memory entry #67):

> **Enforcement belongs in the protocol, not the prompt.** Agents take the path of least resistance — passive hints get ignored, boolean opt-ins drift toward always-on.

Two operational consequences for this rollout:

1. **Cursor tokens, not truncation flags.** When results are capped, return a structured cursor that must be consumed to advance. The server tracks recent (query, owner) pairs and refuses to re-serve identical queries without cursor consumption — blind retries become structurally impossible, not just discouraged.
2. **Reason strings, not boolean opt-ins.** Where verbose / full-body access is needed, gate it behind `verbose: "<reason string>"` instead of `verbose: true`. Adds friction against lazy default-creep and produces auditable logs of when expensive paths are actually used.

---

## Audit Findings (pre-audit code-shape inventory; PR 1 confirms with measurements)

> The table below is derived from reading the current `mcp-server/src/` code, **not** from running the audit. PR 1 Task 1.4 confirms with actual byte/token measurements against a live vault. If the run reveals a tool's behavior diverges from the inventory below (e.g. an undocumented limit), update this table before PR 2 starts.

| Tool | Current default cap | Returns full body? | Biggest risk |
|------|---------------------|--------------------|--------------|
| `search_notes` | `limit: 20` | No (FTS5 snippet) | Cursor needed for >20-result queries |
| `get_note` | n/a (single doc) | Yes | Intentional — explicit body fetch |
| `list_concepts` | `limit: 50` | No (slug + title + description) | Cursor needed past 50 |
| `list_domains` | **no limit** | No (slug + label) | Domains are tiny; low priority |
| `query_graph` | **no default LIMIT** | Depends on caller's SQL | `SELECT * FROM docs` returns entire corpus |
| `get_context` | tiered (minimal/standard/full) | No | Already tiered; needs reason-string for `full` |
| `search_memory` | `limit: 50` | **Yes — full `content`** | 50 entries × ~1KB each = ~50KB per call |

Ordering by ROI: `search_memory` and `query_graph` are highest-impact targets. `search_notes` is the dressed-up base case (already snippet-returning). `list_*` and `get_context` are polish.

---

## PR Sequence Overview

| PR | Scope | Depends on | Detailed plan |
|----|-------|------------|---------------|
| 1 | Audit script + spec doc | — | **This document, below** |
| 2 | Cursor module + reason-string verbose helper (shared infra) | PR 1 | Written after spec lands |
| 3 | `search_memory` adoption (highest ROI) | PR 2 | Written after PR 2 |
| 4 | `query_graph` adoption (cursor + LIMIT injection / refusal) | PR 2 | Written after PR 2 |
| 5 | `search_notes` cursor adoption | PR 2 | Written after PR 2 |
| 6 | `list_concepts` + `list_domains` cursor adoption | PR 2 | Batched; written after PR 2 |
| 7 | `get_context` reason-string opt-in for `depth: "full"` | PR 2 | Written after PR 2 |
| 8 | Migration notes + tool-description updates + final cleanup | PRs 3–7 | Written when most adopters have landed |

**Why this ordering:** PR 2 lands the shared infra so each tool-family PR is small and reviewable. `search_memory` is PR 3 because it's the first real adopter — finding rough edges in the cursor API costs less to fix on one tool than after five tools have copied a flawed pattern. `query_graph` (PR 4) is second-highest-impact but trickier semantically (arbitrary user SQL), so it follows the validated pattern from `search_memory`.

**Why PRs 2–8 don't have full TDD steps in this document:** their step-by-step shape depends on decisions locked in PR 1's spec. Writing fake steps now would create placeholders the skill explicitly forbids. Each PR gets its own detailed plan at `docs/superpowers/plans/2026-05-04-mcp-context-efficiency-pr-N.md` written when its prerequisites have landed.

---

## File Structure (across all PRs)

**PR 1 creates:**
- `scripts/audit_mcp_response_sizes.ts` — reproducible audit harness (TS, runs against a vault path)
- `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md` — design spec
- `docs/superpowers/specs/audit-2026-05-04-mcp-response-sizes.md` — audit results table (linked from spec)

**PR 2 creates:**
- `mcp-server/src/protocol/cursor.ts` — cursor token module (encode, decode, identical-query refusal store)
- `mcp-server/src/protocol/verbose.ts` — reason-string verbose validation helper
- `mcp-server/tests/protocol/cursor.test.ts`
- `mcp-server/tests/protocol/verbose.test.ts`

**PRs 3–7 modify:**
- `mcp-server/src/tools.ts` (one tool per PR)
- `mcp-server/src/sqlite-reader.ts` (paired changes for cursor SQL)
- `mcp-server/src/tool-registry.ts` (input schema additions)
- `mcp-server/tests/tools.test.ts`, `mcp-server/tests/sqlite-reader.test.ts`

**PR 8 modifies:**
- `docs/mcp-setup.md` — caller-facing migration notes
- `mcp-server/src/tool-registry.ts` — final description audit pass

---

# PR 1 — Audit Script + Spec Doc

**Branch:** `feat/issue-50-mcp-efficiency-audit-spec`
**No code-behavior change.** PR 1 is doc-and-tooling-only; nothing in `mcp-server/src/` changes.

**Scope-out (explicit):** PR 1 does NOT touch tool implementations, does NOT add cursor or verbose modules, does NOT change tool-registry schemas. Those are PRs 2+.

## Task 1.1: Branch + plan commit

**Files:**
- Create (commit): `docs/superpowers/plans/2026-05-04-mcp-context-efficiency.md` (this file)

- [ ] **Step 1: Create the branch off latest main**

```bash
git fetch origin
git switch -c feat/issue-50-mcp-efficiency-audit-spec origin/main
```

Expected: branch created, working tree clean except the same untracked items already present (`.gstack/`, `.claude/scheduled_tasks.lock`, `docs/refactor-flatten-spoke-dirs.md`).

- [ ] **Step 2: Commit this plan**

```bash
git add docs/superpowers/plans/2026-05-04-mcp-context-efficiency.md
git commit -m "docs: add multi-PR rollout plan for #50 MCP context efficiency"
```

Expected: one new commit on the branch, no other files staged.

---

## Task 1.2: Audit script — failing test first

**Files:**
- Create: `scripts/audit_mcp_response_sizes.ts`
- Create: `mcp-server/tests/audit-script.test.ts`

The audit script lives at the repo root under `scripts/` (not under `mcp-server/`) because it depends on `mcp-server/dist/*.js` as a *consumer*, not a part of the server. Tests for it run inside the `mcp-server` jest harness because that's where the TS-jest config already lives.

- [ ] **Step 1: Write the failing test for byte-counting helper**

Create `mcp-server/tests/audit-script.test.ts`:

```typescript
import { describe, it, expect } from "@jest/globals";
import { measureResponse } from "../../scripts/audit_mcp_response_sizes.js";

describe("measureResponse", () => {
  it("returns byte length of JSON-serialized response", () => {
    const result = measureResponse({ id: "x", title: "y", snippet: "z" });
    // {"id":"x","title":"y","snippet":"z"} = 36 bytes
    expect(result.bytes).toBe(36);
  });

  it("returns approximate token count using 4-bytes-per-token heuristic", () => {
    const result = measureResponse({ a: "x".repeat(40) });
    // {"a":"xxxx...xxxx"} = 48 bytes ≈ 12 tokens
    expect(result.approxTokens).toBe(12);
  });

  it("handles array responses (e.g. searchNotes return)", () => {
    const result = measureResponse([{ id: "a" }, { id: "b" }]);
    // [{"id":"a"},{"id":"b"}] = 23 bytes
    expect(result.bytes).toBe(23);
    expect(result.entryCount).toBe(2);
  });

  it("reports entryCount: 1 for non-array responses", () => {
    const result = measureResponse({ noteCount: 0 });
    expect(result.entryCount).toBe(1);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd mcp-server && npx jest tests/audit-script.test.ts
```

Expected: FAIL with "Cannot find module '../../scripts/audit_mcp_response_sizes.js'" (TS-jest with ESM imports — the import is the .js compiled name).

- [ ] **Step 3: Implement the audit harness**

Create `scripts/audit_mcp_response_sizes.ts`:

```typescript
/**
 * Reproducible audit of MCP tool response sizes.
 *
 * Usage (from repo root, via mcp-server's npm script):
 *   cd mcp-server && npm run audit -- --vault <path>
 *
 * Output: JSON report on stdout. Convert to a markdown table in
 * docs/superpowers/specs/audit-2026-05-04-mcp-response-sizes.md.
 *
 * The token approximation is intentionally crude (bytes/4). The point is
 * to compare relative sizes across tools, not to predict exact LLM cost.
 */

export interface ResponseMeasurement {
  bytes: number;
  approxTokens: number;
  entryCount: number;
}

export function measureResponse(response: unknown): ResponseMeasurement {
  const json = JSON.stringify(response);
  const bytes = Buffer.byteLength(json, "utf-8");
  const approxTokens = Math.round(bytes / 4);
  const entryCount = Array.isArray(response) ? response.length : 1;
  return { bytes, approxTokens, entryCount };
}

// CLI driver section follows in Task 1.3
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd mcp-server && npx jest tests/audit-script.test.ts
```

Expected: PASS, 4 specs.

- [ ] **Step 5: Commit**

```bash
git add scripts/audit_mcp_response_sizes.ts mcp-server/tests/audit-script.test.ts
git commit -m "feat(audit): add measureResponse helper with byte/token counting"
```

---

## Task 1.3: Audit script — driver that calls each tool

**Files:**
- Modify: `scripts/audit_mcp_response_sizes.ts` (append CLI driver)
- Modify: `mcp-server/tests/audit-script.test.ts` (add driver test against in-memory vault)

The driver imports each tool function from `mcp-server/dist/tools.js`, calls it with realistic input, measures the response, and emits a markdown table. We test the driver end-to-end against a temp vault built from a fixture, NOT a mock — per CLAUDE.md / project memory feedback, integration tests must hit a real DB.

- [ ] **Step 1: Write a failing end-to-end driver test**

Append to `mcp-server/tests/audit-script.test.ts`:

```typescript
import { describe, it, expect, beforeAll, afterAll } from "@jest/globals";
import { runAudit } from "../../scripts/audit_mcp_response_sizes.js";
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import { execSync } from "child_process";

describe("runAudit (end-to-end)", () => {
  let tmpVault: string;

  beforeAll(async () => {
    tmpVault = await fs.mkdtemp(path.join(os.tmpdir(), "schist-audit-"));
    // Build a minimal vault: schist init + a few notes
    execSync(`schist init ${tmpVault} --name audit-test`, { stdio: "pipe" });
    for (let i = 0; i < 5; i++) {
      const noteFile = path.join(tmpVault, "notes", `2026-05-04-fixture-${i}.md`);
      await fs.mkdir(path.dirname(noteFile), { recursive: true });
      await fs.writeFile(
        noteFile,
        `---\ntitle: Fixture ${i}\ndate: 2026-05-04\nstatus: draft\ntags: [audit]\n---\n\nBody for fixture note ${i}, ${"x".repeat(200)}.\n`
      );
    }
    execSync(`cd ${tmpVault} && git add -A && git commit -m "fixtures"`, { stdio: "pipe" });
    execSync(`schist-ingest --vault ${tmpVault} --db ${tmpVault}/.schist/schist.db`, { stdio: "pipe" });
  });

  afterAll(async () => {
    await fs.rm(tmpVault, { recursive: true, force: true });
  });

  it("produces a report covering every tool in the audit set", async () => {
    const report = await runAudit({ vault: tmpVault });
    expect(report.tools).toEqual(
      expect.arrayContaining([
        "search_notes",
        "list_concepts",
        "list_domains",
        "query_graph",
        "get_context",
        "search_memory",
      ])
    );
  });

  it("reports search_notes byte count > 0 against fixture vault", async () => {
    const report = await runAudit({ vault: tmpVault });
    const sn = report.measurements.search_notes;
    expect(sn.bytes).toBeGreaterThan(0);
    expect(sn.entryCount).toBeGreaterThanOrEqual(1);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd mcp-server && npx jest tests/audit-script.test.ts -t runAudit
```

Expected: FAIL — `runAudit` not exported.

- [ ] **Step 3: Implement runAudit**

Append to `scripts/audit_mcp_response_sizes.ts`:

```typescript
import * as tools from "../mcp-server/dist/tools.js";

export interface AuditReport {
  vault: string;
  generatedAt: string;
  tools: string[];
  measurements: Record<string, ResponseMeasurement>;
}

export async function runAudit(opts: { vault: string }): Promise<AuditReport> {
  const measurements: Record<string, ResponseMeasurement> = {};

  // search_notes — typical "find what I worked on" query
  measurements.search_notes = measureResponse(
    await tools.search_notes(opts.vault, { query: "fixture" })
  );

  // list_concepts — unbounded by default, capture worst case
  measurements.list_concepts = measureResponse(
    await tools.list_concepts(opts.vault, {})
  );

  // list_domains — no limit at all
  measurements.list_domains = measureResponse(
    await tools.list_domains(opts.vault, {})
  );

  // query_graph — the unbounded SELECT case from issue #50
  measurements.query_graph = measureResponse(
    await tools.query_graph(opts.vault, { sql: "SELECT * FROM docs" })
  );

  // get_context — measure all three depths
  for (const depth of ["minimal", "standard", "full"] as const) {
    measurements[`get_context_${depth}`] = measureResponse(
      await tools.get_context(opts.vault, { depth })
    );
  }

  // search_memory — long-form content, default 50 limit
  measurements.search_memory = measureResponse(
    await tools.search_memory(opts.vault, { limit: 50 })
  );

  return {
    vault: opts.vault,
    generatedAt: new Date().toISOString(),
    tools: Object.keys(measurements),
    measurements,
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const vaultIdx = process.argv.indexOf("--vault");
  if (vaultIdx === -1) {
    console.error("Usage: tsx scripts/audit_mcp_response_sizes.ts --vault <path>");
    process.exit(2);
  }
  const vault = process.argv[vaultIdx + 1];
  runAudit({ vault }).then((r) => console.log(JSON.stringify(r, null, 2)));
}
```

- [ ] **Step 4: Build mcp-server (driver imports compiled .js)**

```bash
cd mcp-server && npm run build
```

Expected: `dist/` populated, no TS errors.

- [ ] **Step 5: Run the driver test**

```bash
cd mcp-server && npx jest tests/audit-script.test.ts -t runAudit
```

Expected: PASS, 2 specs.

- [ ] **Step 6: Commit**

```bash
git add scripts/audit_mcp_response_sizes.ts mcp-server/tests/audit-script.test.ts
git commit -m "feat(audit): add runAudit driver covering all 6 read-side tools"
```

---

## Task 1.4: Run the audit against a real vault, capture results

**Files:**
- Create: `docs/superpowers/specs/audit-2026-05-04-mcp-response-sizes.md`

This task uses the local HPC schist-vault (`~/schist-vault`) as the realistic-vault fixture. That vault has accumulated notes/concepts/edges across multiple spokes since the 2026-05-02 flatten refactor and gives us a non-trivial baseline. (If running on a different machine, substitute the local vault path.)

- [ ] **Step 0: Ensure tsx is available**

`tsx` is not yet a project dep. Add it to `mcp-server/devDependencies` and add an `audit` npm script so the invocation has a stable home:

```bash
cd mcp-server && npm install --save-dev tsx
# package.json: add to "scripts":  "audit": "tsx ../scripts/audit_mcp_response_sizes.ts"
```

Commit the package.json + lockfile change separately (`build: add tsx for audit script`) so it's reviewable on its own.

- [ ] **Step 1: Run the audit and save raw output**

```bash
cd mcp-server && npm run audit -- --vault ~/schist-vault > /tmp/audit-raw.json
```

Expected: JSON output, byte counts > 0 for every tool. If `search_memory` returns 0 entries the local memory DB is empty — pass `--vault` to a vault that has memory data, OR populate the memory DB with a fixture before running. Document whichever path was used in the result file.

- [ ] **Step 2: Convert raw output to markdown table**

Create `docs/superpowers/specs/audit-2026-05-04-mcp-response-sizes.md`:

```markdown
# MCP Tool Response Size Audit — 2026-05-04

**Vault audited:** `~/schist-vault` (HPC spoke, post-flatten)
**Audit script:** `scripts/audit_mcp_response_sizes.ts`
**Reproduce:** `cd mcp-server && npm run audit -- --vault <path>`

## Summary

| Tool | Bytes | ≈ Tokens | Entries | Notes |
|------|-------|----------|---------|-------|
| `search_notes` (query="fixture") | <fill> | <fill> | <fill> | FTS5 snippet, default limit 20 |
| `list_concepts` (no opts) | <fill> | <fill> | <fill> | Default limit 50 |
| `list_domains` (no opts) | <fill> | <fill> | <fill> | **No limit** |
| `query_graph` (`SELECT * FROM docs`) | <fill> | <fill> | <fill> | **No default LIMIT** — worst case |
| `get_context` (minimal) | <fill> | <fill> | 1 | Counts only |
| `get_context` (standard) | <fill> | <fill> | 1 | + recent + hotConcepts |
| `get_context` (full) | <fill> | <fill> | 1 | + tagCloud (top 30 tags) |
| `search_memory` (limit=50) | <fill> | <fill> | <fill> | **Returns full content field** |

## Per-tool observations

[Fill in per-tool obs after running. Specifically capture:]
- Which tools clear ~10KB / ~2.5K tokens — those are the ones the spec must address
- Which tools are already in good shape — note explicitly so the spec doesn't over-engineer
- Anomalies (e.g. one tool returns 100x more than expected — investigate)

## Reproduction notes

- Date the vault contained at audit time: <fill>
- `git rev-parse HEAD` of mcp-server: <fill>
- Node version: <fill>

The audit script is committed; re-running it after each implementation PR
gives a regression metric. Keep the markdown table in this file as the
canonical historical record — append new dated tables, do not overwrite.
```

Fill in each `<fill>` from `/tmp/audit-raw.json`.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/audit-2026-05-04-mcp-response-sizes.md
git commit -m "docs(audit): capture 2026-05-04 baseline MCP response sizes"
```

---

## Task 1.5: Write the design spec

**Files:**
- Create: `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md`

The spec is the contract every implementation PR (2–8) references during review. It locks in protocol decisions before any tool changes.

- [ ] **Step 1: Draft the spec**

Create `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md` with these sections:

```markdown
# MCP Context Efficiency — Design Spec

**Tracks:** [yibeichan/schist#50](https://github.com/yibeichan/schist/issues/50)
**Implementation plan:** `docs/superpowers/plans/2026-05-04-mcp-context-efficiency.md`
**Audit baseline:** `docs/superpowers/specs/audit-2026-05-04-mcp-response-sizes.md`

## Goal

Reduce agent context burn from MCP tool responses, especially as the
vault grows. Adopt cursor-based pagination and reason-string opt-ins so
agents can't fall back to default-bloat behavior.

## Core principles

[Restate "enforcement in protocol, not prompt" + the two patterns,
copying language from this plan's Guiding Principles section so the
spec stands alone.]

## Cursor protocol

### Token shape

A cursor is an opaque (to the agent), HMAC-signed JSON payload:

```json
{
  "tool": "search_notes",
  "queryHash": "<sha256 of (sorted-args + owner)>",
  "offset": 20,
  "issuedAt": 1717459200,
  "ttlSeconds": 300
}
```

Encoded as base64url, signed with a per-server-instance HMAC secret
(generated at process start, not persisted). Agents pass it as
`{ cursor: "<token>" }` on follow-up calls.

### Server-side identical-query refusal

When a tool returns a cursor, the server stores `(queryHash, owner, issuedAt)`
in an in-memory LRU (size 256). On a subsequent call:

| Condition | Behavior |
|-----------|----------|
| Same `queryHash` + `owner` within `ttlSeconds`, NO cursor passed | Return `error: "CURSOR_REQUIRED", message: "Identical query within ${ttl}s — pass cursor or refine"`, with the original cursor token re-attached. |
| Same `queryHash` + `owner`, cursor passed and validated | Serve next page; advance LRU entry. |
| Different `queryHash` (refined query) | Treated as a new query; previous cursor expires. |
| TTL expired | Treat as new query; emit a soft note in response so debugging is easy. |

### Tool-specific cursor adoption

[For each of search_notes, query_graph, search_memory, list_concepts, list_domains:
specify what `queryHash` includes, what `offset` semantics are, what the
"next page" SQL shape looks like.]

## Reason-string verbose

Tools that have a "give me more" mode replace `verbose: boolean` with `verbose: string`.

- The string must be ≥ 12 characters (filters out lazy `verbose: "x"` workarounds).
- The string is logged to the MCP server's stderr at INFO level for audit.
- Empty string or omitted field → not verbose. No silent fallback.

Tools adopting reason-string verbose:
- `get_context` for `depth: "full"` — currently `full` triggers tagCloud computation; gating it is cheap value.
- `search_memory` for full-content return (default returns content snippet).
- (Future-proof: any tool that grows a "give me everything" mode.)

`search_notes` does NOT need reason-string verbose — full bodies are obtained via `get_note`, which is already an explicit two-step.

## Default limits

[For each tool, document: current default, proposed default, reasoning.
Most are already sensible; `list_domains` and `query_graph` are the changes.]

## Compatibility / migration

- All current MCP clients call these tools without `cursor` and without
  `verbose`. Both fields are added as **optional inputs** in tool-registry
  schemas, so existing callers continue working — they just may receive
  a `cursor` field they ignore (and, if they retry identically, a new
  `CURSOR_REQUIRED` error).
- The `CURSOR_REQUIRED` error is the user-visible breaking change. Mitigate
  by:
  - Updating every tool's description string in `tool-registry.ts` to
    mention pagination (so agents read the rule from the input schema).
  - Updating `docs/mcp-setup.md` with the new convention.
  - Calling out the change in the next release notes.

## Out-of-scope for this rollout

- Authentication / authorization changes (write auth is enforced by
  `validateOwner` against `SCHIST_AGENT_ID` / `SCHIST_ALLOWED_AGENTS`;
  the pre-#72 `request_capabilities` gate was removed as it provided no
  real access control).
- Streaming responses (would require MCP protocol-level changes).
- Caching tool responses across sessions (out of scope; orthogonal).
```

- [ ] **Step 2: Self-review the spec**

Read the spec end-to-end. Check:
- Every implementation PR (2–8) has a paragraph it can point at for "what should I build?"
- The cursor format is specified concretely enough that PR 2 can implement it from the spec alone (no tribal knowledge required).
- The reason-string rules are unambiguous (≥12 chars, logged, no silent fallback).
- The "compatibility" section names the breaking change explicitly.

If any of those are missing, edit until they are.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md
git commit -m "docs(spec): MCP context efficiency design (#50)"
```

---

## Task 1.6: Open PR for review

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/issue-50-mcp-efficiency-audit-spec
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "docs+audit: MCP context efficiency baseline + spec (#50)" --body "$(cat <<'EOF'
## Summary
- Reproducible audit script (`scripts/audit_mcp_response_sizes.ts`) with byte/token measurements per MCP tool
- Captured 2026-05-04 baseline measurements against the HPC vault
- Design spec for cursor-based pagination + reason-string verbose, building on m13v's comment on #50
- Multi-PR rollout plan covering PRs 2–8 (cursor infra, then per-tool adoption)

No behavior change in this PR — purely audit + spec + plan. Implementation lands in PR 2 onwards.

## Test plan
- [ ] `npm test` passes in `mcp-server/`
- [ ] Audit script runs cleanly against a fresh vault (`schist init` + a few notes)
- [ ] Audit script runs cleanly against the live HPC vault
- [ ] Spec self-review: every PR 2–8 has a section it can reference

## Refs
Closes part of #50 (audit + spec milestone). Implementation tracked in follow-up PRs.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Verify CI green**

```bash
gh pr checks
```

Expected: all checks pass. If they don't, fix in-place — do NOT merge through red.

- [ ] **Step 4: Wait for human review**

Tag yibei (the human owner) for spec review specifically. Implementation PRs depend on the spec being accepted.

---

# PRs 2–8 — Outline Only

Each gets its own detailed plan written when its prerequisites land. The structure below is the scope contract for each PR; detailed steps come later.

## PR 2 — Cursor module + reason-string helper

**Purpose:** Land shared infrastructure so PRs 3–7 are small.

**Scope:**
- `mcp-server/src/protocol/cursor.ts` exports: `issueCursor(args)`, `validateCursor(token)`, `recordIssued(queryHash, owner)`, `checkIdentical(queryHash, owner)`, `clearExpired()`.
- `mcp-server/src/protocol/verbose.ts` exports: `parseVerbose(input)` returning `{ enabled: boolean, reason?: string }`.
- LRU storage is in-process Map, capped at 256 entries; cleared on process restart (acceptable since cursors are TTL-bound at 300s).
- Full Jest coverage: cursor encode/decode/expiry, identical-query refusal logic, reason-string validation (length, whitespace handling).

**Out of scope:** No tool actually uses these modules in PR 2. Wiring lands in PRs 3–7.

**Detailed plan:** Written after PR 1 spec is accepted, saved as `docs/superpowers/plans/2026-05-04-mcp-context-efficiency-pr-2.md`.

## PR 3 — `search_memory` cursor + reason-string verbose adoption

**Why first adopter:** Highest-impact (returns full `content`) and clearest verbose case (snippet by default, full content via reason string).

**Scope:**
- `tools.ts`: `search_memory` accepts `cursor` and `verbose: string`. Returns `{ entries: [...], cursor?: token, truncated: boolean }`.
- `sqlite-reader.ts`: `searchMemory` returns content snippets (first ~200 chars) by default; full content only when verbose reason is non-empty.
- `tool-registry.ts`: input schema updated to document the new fields.
- Tests: cursor pagination (page-1, page-2, end-of-results), identical-query refusal, snippet vs full-content modes, ≥12-char reason validation.

**Detailed plan:** Written after PR 2 lands, as `docs/superpowers/plans/2026-05-04-mcp-context-efficiency-pr-3.md`.

## PR 4 — `query_graph` cursor + LIMIT injection

**Why second:** Highest-impact after `search_memory`; unbounded SELECT is a real footgun.

**Scope:**
- Server-side: if caller's SQL has no `LIMIT`, inject `LIMIT 100`. If the caller did include `LIMIT`, respect it but cap at 1000.
- Cursor encodes `OFFSET` for re-issue with the same SQL (queryHash includes the SQL string + params).
- Refuse identical-SQL re-issue without cursor (catches the "agent retries same SELECT * FROM docs" pattern m13v warned about).
- Tests: bare SELECT gets default LIMIT, large SELECT gets capped, cursor advances OFFSET, identical-query refusal works for SQL too.

**Detailed plan:** as PR 3, post-PR-2.

## PR 5 — `search_notes` cursor adoption

**Scope:** Lighter than 3/4 — `search_notes` already returns snippets and has a default limit. Just adds cursor pagination and identical-query refusal. No verbose changes (full bodies via `get_note` continues to be the explicit path).

**Detailed plan:** as above.

## PR 6 — `list_concepts` + `list_domains` cursor adoption

**Why batched:** Both are low-content list endpoints with similar shapes. One PR, two tools.

**Scope:**
- `list_domains` gets a default `limit: 100` (was unlimited).
- Both tools accept `cursor` and return one when `truncated: true`.
- `list_concepts` already has limit 50 — preserved.

**Detailed plan:** as above.

## PR 7 — `get_context` reason-string opt-in for `depth: "full"`

**Scope:**
- `depth: "full"` gated behind a reason-string parameter (e.g. `fullReason: "<≥12 chars>"`). Without it, the server downgrades to `standard` and emits a hint.
- `depth: "minimal"` and `depth: "standard"` unchanged.
- The `tagCloud` computation in `getContext` is the actual cost being gated.

**Detailed plan:** as above.

## PR 8 — Migration notes + tool-description updates + cleanup

**Scope:**
- `docs/mcp-setup.md` — caller-facing notes on cursor, reason-string, breaking changes.
- `tool-registry.ts` — pass over every tool description to mention cursor / pagination where applicable, in agent-readable language.
- Re-run the audit script, append the post-rollout numbers to `docs/superpowers/specs/audit-2026-05-04-mcp-response-sizes.md` for delta documentation.
- Close issue #50.

**Detailed plan:** as above.

---

# Locked Decisions (2026-05-10)

Locked pre-PR-1 in a scoping session. The spec doc (Task 1.5) restates these as normative; this section preserves the rationale. Changes ripple expensively, so lock-in is the gate.

1. **Cursor TTL = 300s.** ✅ Long enough for normal multi-page agent loops; short enough that stale cursors don't pile up.
2. **HMAC secret = per-process, no persistence.** ✅ Cursors are inherently in-session; persisting would add storage + rotation complexity for no real benefit. Cursors die on server restart, which is acceptable.
3. **Reason-string minimum = 12 chars.** ✅ Real friction against the lazy `verbose: "x"` pattern without being painful (~two short words). Going to 20 wouldn't catch much more — agents adapt.
4. **Verbose-content tools = `get_context (full)` + `search_memory`.** ✅ `search_notes` deliberately excluded — `get_note` is already an explicit two-step path for full bodies, which is the better protocol pattern than verbose-flag.
5. **`query_graph` LIMIT: default 100, caller cap 1000.** ✅ ⚠️ **Behavior change.** Today `SELECT * FROM docs` is unbounded. After PR 4, the same query returns 100 rows + a cursor. Power users who write `LIMIT 5000` get capped at 1000. PR 4 commit message + PR 8 migration notes must call this out explicitly.

---

# Self-Review (run before opening PR 1)

After this plan is committed:

**1. Spec coverage of issue #50.** Walk through each "Likely sources of bloat" hypothesis in #50:
- ✅ `search_notes` — covered by PR 5.
- ✅ `get_context` — covered by PR 7.
- ✅ `query_graph` — covered by PR 4.
- ✅ `list_concepts` / `list_domains` — covered by PR 6.
- ✅ `search_memory` — covered by PR 3.

**2. Placeholder scan.** Searched this document for "TBD", "TODO", "implement later", "fill in later" — only intentional `<fill>` placeholders in the audit-results task (filled at run time, not at plan-write time).

**3. Type consistency.** Functions referenced in this plan:
- `measureResponse(response): ResponseMeasurement` — defined Task 1.2, used Task 1.3.
- `runAudit({ vault }): AuditReport` — defined Task 1.3, called by tests in Task 1.3 and CLI in Task 1.4.
- `issueCursor / validateCursor / parseVerbose` — described in PR 2 outline; signatures locked there, not assumed by PR 1.

No type drift across tasks within PR 1.

**4. Untracked-files hygiene.** `git status` at the start of PR 1 must not stage:
- `.gstack/`
- `.claude/scheduled_tasks.lock`
- `docs/refactor-flatten-spoke-dirs.md`

These are persistent cross-session artifacts. The plan's commits are scoped to specific paths, never `git add -A`.
