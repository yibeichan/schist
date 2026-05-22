# PR 7 Plan — `get_context` reason-string verbose adoption (Issue #50)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt **reason-string verbose** on `get_context` for `depth: "full"` — the last cursor-protocol adopter in the Issue #50 rollout. `get_context` does NOT paginate (spec §"cursor-input table" explicitly: "fixed-shape summary, not a list"); the entire PR 7 surface is the verbose gate. Without a valid verbose reason, `depth: "full"` silently downgrades to `depth: "standard"` and the response carries a `verboseNote` hint.

**Architecture:** The simplest of the seven protocol-adopting PRs. No SQL changes, no cursor pipeline, no shape-breaking response wrap. (1) Type layer: add `GetContextResponse` interface to `types.ts` so the handler return type is explicit. (2) Tool-handler layer: thread `parseVerbose` → effective-depth resolution → existing SQLite read → optional `logVerbose` + `noteHighFrequency`. (3) Registry layer: add `verbose` input to `get_context` schema in `tool-registry.ts`. No changes to `sqlite-reader.getContext`.

**Tech Stack:** TypeScript (strict, ESM, Node ≥20). No new runtime dependencies. Jest 30 + ts-jest ESM preset.

**Tracks:** [yibeichan/schist#50](https://github.com/yibeichan/schist/issues/50) (PR 7 of 8-PR rollout). Spec contract: `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md` §"Reason-string verbose" and §"Tools adopting reason-string verbose (locked)".

**Baseline tests:** 20 suites / 340 tests green (post-#107 merge, 2026-05-22).

---

## Spec → task mapping

| Spec section | PR 7 task |
|---|---|
| §"Tools adopting reason-string verbose (locked)": `get_context` for `depth: "full"` — without reason, server downgrades to `standard` and emits a soft hint | Task 7.1 (handler) |
| §"Reason-string verbose" — `verbose: string` not `verbose: boolean`; `≥12 code points after trim`; `verbose: true` rejected as INVALID_ARG type error | Task 7.1 (parseVerbose call) |
| §"Reason-string verbose" — stderr audit log line: `[verbose] <tool> by <owner\|anonymous>: <JSON.stringify(reason)>` | Task 7.1 (logVerbose call) |
| §"Rate-limit note (PR 2 + PR 3 + PR 7)" — `>5` verbose calls per minute per (owner, tool) get a soft `verboseNote: "high-frequency verbose use detected"` | Task 7.1 (noteHighFrequency call) |
| §"Default limits" — `get_context`: tiered (minimal / standard / full), `full` requires verbose reason | Task 7.1 (downgrade logic) |
| Tool-registry: add `verbose` input schema with description | Task 7.2 |
| Type: `GetContextResponse` interface in `types.ts` | Task 7.3 |
| Audit re-measurement (depth-tier byte counts) | Task 7.4 |
| CHANGELOG entry — behavior change | Task 7.5 |

---

## Behavior change specification

### Today (pre-PR 7)

```typescript
// args: { depth?: "minimal" | "standard" | "full" }
get_context(vault, { depth: "full" })
// Returns: { vault, recent, hotConcepts, tagCloud, syncWarning? }
// tagCloud computation runs unconditionally.
```

### After PR 7

```typescript
// args: { depth?: "minimal" | "standard" | "full"; verbose?: string }

// Case A: depth=full + valid verbose (≥12 cp, after trim)
get_context(vault, { depth: "full", verbose: "preparing handoff doc" })
// Returns: { vault, recent, hotConcepts, tagCloud, syncWarning?, verboseNote? }
// tagCloud computation runs. logVerbose fires. noteHighFrequency may set verboseNote.

// Case B: depth=full + missing/invalid-length verbose
get_context(vault, { depth: "full" })
// Server-side: downgrade depth to "standard". Run standard query.
// Returns: { vault, recent, hotConcepts, syncWarning?, verboseNote: "depth=full requires verbose: \"<reason ≥12 chars>\"; downgraded to standard" }
// NO tagCloud field. NO logVerbose. NO INVALID_ARG error — graceful soft-downgrade per spec.

// Case C: depth=full + verbose: true (boolean type error)
get_context(vault, { depth: "full", verbose: true })
// Returns: { error: "INVALID_ARG", message: "verbose must be a string reason (≥12 code points); got boolean" }
// Hard error — type misuse, not a missing-reason graceful path.

// Case D: depth=standard or "minimal" + verbose set
get_context(vault, { depth: "standard", verbose: "..." })
// verbose is validated for type (rejects boolean/non-string). If valid, IGNORED — no logVerbose, no semantic effect. Same response as today.

// Case E: depth omitted (defaults to "minimal") + verbose set
// Same as Case D — verbose validated, ignored.
```

### Spec rationale (cited)

Spec line 373–376:

> **`get_context`** for `depth: "full"` — currently `full` triggers `tagCloud` computation. Without a reason, the server downgrades to `standard` and emits a soft hint. Gating is cheap and the cost is computational, not just bytes.

The soft-downgrade (not hard-error) is the deliberate choice: agents that lazily request `depth: "full"` get a useful response (standard) plus a hint that "next time, give me a reason if you actually need the tagCloud."

---

## Files

### `mcp-server/src/tools.ts` — `get_context` handler (currently lines 745–780)

**Imports to add (already imported for other handlers — verify):**
- `parseVerbose, logVerbose, noteHighFrequency` from `./protocol/index.js` (already present, used by `search_memory`)
- `GetContextResponse` type from `./types.js`

**Before (lines 745–780):**

```typescript
export async function get_context(
  vaultRoot: string,
  // Default to "minimal" for agent session-start: only note/concept/edge counts
  // + last 3 modified. Agents that need richer context request standard/full.
  args: { depth?: "minimal" | "standard" | "full" }
): Promise<unknown> {
  await maybeSpokePull(vaultRoot);

  // Read (and clear) any pending background-sync-failure sentinel so agents
  // don't silently work against a stale local view. This runs independently
  // of the SQLite read — even if the DB query fails, we surface the warning
  // on the error result so the operator knows to check the hub connection.
  let syncWarning: string | undefined;
  const sentinelPath = path.join(vaultRoot, SYNC_ERROR_SENTINEL);
  try {
    const errText = (await fs.readFile(sentinelPath, "utf-8")).trim();
    if (errText) {
      syncWarning = `Recent background sync failure: ${errText}. Writes may not have reached the hub.`;
      await fs.unlink(sentinelPath).catch(() => {});
    }
  } catch {
    // No sentinel — healthy state
  }

  try {
    const context = sqliteReader.getContext(vaultRoot, args.depth ?? "minimal") as Record<string, unknown>;
    if (syncWarning) context.syncWarning = syncWarning;
    return context;
  } catch (e: unknown) {
    const err = normalizeError(e, "INGEST_ERROR");
    if (syncWarning) {
      return { ...err, syncWarning };
    }
    return err;
  }
}
```

**After:**

```typescript
/**
 * get_context tool handler. Adopts reason-string verbose (PR 7 of #50):
 *
 *   parseVerbose → effective-depth resolution → spoke pull → SQLite read →
 *   optional logVerbose + noteHighFrequency on verbose-gated full responses.
 *
 * Soft-downgrade semantics (spec §"Reason-string verbose"):
 *   - depth="full" + valid verbose (≥12 cp) → run tagCloud, log audit line.
 *   - depth="full" + missing/short verbose → silently run as depth="standard"
 *     and attach a verboseNote hinting at the upgrade path. NOT an error —
 *     callers that lazily ask for "full" should still get a usable response.
 *   - depth!="full" + verbose set → verbose validated for type only; ignored
 *     semantically. Matches the pattern in search_memory.
 *
 * Spec: docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md
 */
export async function get_context(
  vaultRoot: string,
  args: { depth?: "minimal" | "standard" | "full"; verbose?: string }
): Promise<GetContextResponse | ToolError> {
  const TOOL_NAME = "get_context" as const;

  // Step 1: parseVerbose. Reject INVALID_ARG (boolean / non-string / too-short)
  // before any I/O. parseVerbose returns { enabled: false } for omitted/empty
  // input (treated as missing — fine for non-"full" depths) and an error
  // variant for type misuse.
  const v = parseVerbose(args.verbose);
  if ("error" in v) return v.error;
  const verboseEnabled = v.enabled;
  const verboseReason: string | undefined = v.enabled ? v.reason : undefined;

  // Step 2: effective depth resolution. If the caller asked for "full" but
  // didn't supply a valid verbose reason, downgrade to "standard" and prepare
  // a soft hint. Any other (depth, verbose) combination passes through.
  const requestedDepth = args.depth ?? "minimal";
  let effectiveDepth: "minimal" | "standard" | "full" = requestedDepth;
  let downgradeNote: string | undefined;
  if (requestedDepth === "full" && !verboseEnabled) {
    effectiveDepth = "standard";
    downgradeNote =
      'depth="full" requires verbose: "<reason ≥12 chars>"; downgraded to "standard"';
  }

  await maybeSpokePull(vaultRoot);

  // Step 3: background-sync sentinel (unchanged from prior behaviour).
  let syncWarning: string | undefined;
  const sentinelPath = path.join(vaultRoot, SYNC_ERROR_SENTINEL);
  try {
    const errText = (await fs.readFile(sentinelPath, "utf-8")).trim();
    if (errText) {
      syncWarning = `Recent background sync failure: ${errText}. Writes may not have reached the hub.`;
      await fs.unlink(sentinelPath).catch(() => {});
    }
  } catch {
    // No sentinel — healthy state
  }

  // Step 4: SQLite read at effectiveDepth.
  let context: Record<string, unknown>;
  try {
    context = sqliteReader.getContext(vaultRoot, effectiveDepth) as Record<string, unknown>;
  } catch (e: unknown) {
    const err = normalizeError(e, "INGEST_ERROR");
    return syncWarning ? { ...err, syncWarning } : err;
  }
  if (syncWarning) context.syncWarning = syncWarning;

  // Step 5: verbose audit log + rate-limit hint (only on true depth="full" path).
  let freqNote: string | undefined;
  const activeOwner = process.env.SCHIST_AGENT_ID ?? "";
  if (effectiveDepth === "full" && verboseEnabled && verboseReason !== undefined) {
    logVerbose({ tool: TOOL_NAME, owner: activeOwner, reason: verboseReason });
    const note = noteHighFrequency({
      tool: TOOL_NAME,
      owner: activeOwner,
      reason: verboseReason,
    });
    if (note !== null) freqNote = note;
  }

  // Step 6: assemble response. verboseNote is set if either (a) the call was
  // downgraded, or (b) the rate-limit tracker fired. Concatenate when both.
  const verboseNote =
    downgradeNote !== undefined && freqNote !== undefined
      ? `${downgradeNote}; ${freqNote}`
      : downgradeNote ?? freqNote;
  if (verboseNote !== undefined) context.verboseNote = verboseNote;

  return context as GetContextResponse;
}
```

### `mcp-server/src/types.ts` — add `GetContextResponse`

**After existing `ListConceptsResponse` / `ListDomainsResponse` interfaces:**

```typescript
/**
 * Response shape for the `get_context` tool. The handler returns either
 * this object or a `ToolError`. All inner fields are optional because
 * the SQLite-reader's `getContext()` returns different shapes at each
 * depth tier (minimal / standard / full).
 */
export interface GetContextResponse {
  // Always present at every depth:
  noteCount?: number;
  conceptCount?: number;
  edgeCount?: number;
  // depth >= "standard":
  vault?: { path: string; noteCount: number; conceptCount: number; edgeCount: number };
  recent?: Array<Record<string, unknown>>;
  hotConcepts?: Array<Record<string, unknown>>;
  // depth === "full":
  tagCloud?: Array<{ tag: string; count: number }>;
  // Operational hints (set independently of depth):
  syncWarning?: string;
  verboseNote?: string;
}
```

### `mcp-server/src/tool-registry.ts` — `get_context` schema

Find the `get_context` entry. Update description and inputSchema:

**Before:**

```typescript
{
  name: "get_context",
  description: "Get vault context: counts, recent docs, hot concepts.",
  inputSchema: {
    type: "object" as const,
    properties: {
      depth: { type: "string", enum: ["minimal", "standard", "full"] },
    },
  },
},
```

**After:**

```typescript
{
  name: "get_context",
  description: 'Get vault context: counts, recent docs, hot concepts. depth: "full" additionally returns tagCloud and requires verbose: "<reason ≥12 chars>"; without a valid reason the server downgrades to "standard" and emits a verboseNote hint.',
  inputSchema: {
    type: "object" as const,
    properties: {
      depth: { type: "string", enum: ["minimal", "standard", "full"] },
      verbose: {
        type: "string",
        description: 'Reason string (≥12 code points after trim) gating depth="full". Logged to server stderr for audit. Omit or use a non-"full" depth if not needed.',
      },
    },
  },
},
```

### `mcp-server/tests/get-context-tool.test.ts` — NEW

Test cases (one per Case in the behavior-change spec):

| # | Setup | Args | Expected |
|---|-------|------|----------|
| 1 | seeded vault, fixture corpus | `{ depth: "full", verbose: "long enough reason here" }` | response has `tagCloud`, NO `verboseNote` describing downgrade; logVerbose called |
| 2 | seeded vault | `{ depth: "full" }` | response shaped as standard (NO `tagCloud`), `verboseNote` says downgraded |
| 3 | seeded vault | `{ depth: "full", verbose: "too short" }` (8 cp) | INVALID_ARG via parseVerbose (≥12 cp required) — NOT downgrade |
| 4 | seeded vault | `{ depth: "full", verbose: true }` as never | INVALID_ARG: "verbose must be a string" |
| 5 | seeded vault | `{ depth: "standard", verbose: "valid long reason" }` | standard response, NO `verboseNote`, verbose IGNORED (no logVerbose) |
| 6 | seeded vault | `{ depth: "minimal" }` | minimal counts-only shape, unchanged from today |
| 7 | seeded vault | rate-limit primitive: call `noteHighFrequency` directly 30× then once more (or stub `VERBOSE_RATE_LIMIT_PER_MIN` via the verbose.ts module if reachable in tests) | the (31st) trigger returns the high-frequency note. Verifies wiring; the protocol-level rate-limit math is already covered by `protocol/verbose.test.ts`. |
| 8 | seeded vault, sentinel file present | `{ depth: "full" }` | response has `syncWarning` AND `verboseNote` (downgrade); sentinel deleted |
| 9 | seeded vault, corrupt DB | `{ depth: "full", verbose: "long reason" }` | INGEST_ERROR returned (matches search_memory error fallthrough pattern) |
| 10 | seeded vault | `{ depth: "minimal", verbose: true }` as never | INVALID_ARG — type validation applies regardless of depth (no depth check around parseVerbose) |
| 11 | seeded vault | `{ depth: "full", verbose: "   " }` (whitespace only) | depth downgraded to "standard", `verboseNote` present — `parseVerbose` returns `{enabled: false}` (NOT error) for whitespace-only strings (per protocol/verbose.ts:56) |
| 12 | seeded vault | depth=full, valid verbose, called 31× (or rate-limit primitive stubbed) | response carries `verboseNote` containing BOTH `'depth="full" requires...'` AND the high-frequency hint, joined with `;` separator. Covers the concat branch in step 6 of the handler. |

Test cases #3 (too-short reason) and #7 (rate-limit) use `parseVerbose` and `noteHighFrequency` internals — the existing verbose protocol tests at `protocol/verbose.test.ts` already cover those primitives, so the get_context tests just verify wiring.

### `CHANGELOG.md` — add entry

**Under `[Unreleased] > Changed`:**

```markdown
- **BEHAVIOR CHANGE:** `get_context` MCP tool — `depth: "full"` now requires
  `verbose: "<reason ≥12 chars>"` to actually compute the `tagCloud` field.
  Without a valid reason, the server silently downgrades to `depth: "standard"`
  and the response carries a `verboseNote` hint indicating the upgrade path.
  No error is raised — agents that lazily ask for "full" still get a usable
  standard response. The `verbose` field is also validated for type on all
  depths (e.g. `verbose: true` is rejected as INVALID_ARG). Refs #50 (PR 7
  of the context-efficiency rollout).
```

### `docs/superpowers/specs/audit-2026-05-22-mcp-response-sizes-pr7.md` — new audit doc

Run `npm run audit --prefix mcp-server -- --vault <local vault>` and compare get_context (minimal/standard/full) byte counts against PR 6's snapshot (`audit-2026-05-20-mcp-response-sizes-pr6.md`). Expectation: minimal/standard unchanged; full unchanged when reason is supplied; full+no-reason measures `~standard byte count + verboseNote overhead` (i.e. shrinks by the tagCloud delta).

If local vault is unavailable, document that the audit will be folded into a later doc and provide a synthetic-fixture measurement instead.

---

## Tasks

Each task is intentionally small + locally testable. Run `npm run build && npm test` after each.

- [ ] **Task 7.0 — Verify baseline** (5 min CC)
  - Run `npm test` from `mcp-server/`. Expect 340/340 green.
  - Confirm branch `feat/issue-50-get-context-verbose` is on top of `main` HEAD `9a469f5`.

- [ ] **Task 7.1 — Implement get_context handler** (15 min CC)
  - Update `mcp-server/src/tools.ts` `get_context` per the "After" block above.
  - Add the imports for `parseVerbose`, `logVerbose`, `noteHighFrequency` if not already imported (search_memory already imports them; reuse).
  - Run `npm run build` — expect 0 TS errors.

- [ ] **Task 7.2 — Tool-registry schema** (3 min CC)
  - Update `mcp-server/src/tool-registry.ts` `get_context` entry per the "After" block above.
  - Run `npm run build` again — registry schema changes don't affect TS compile but verify clean.

- [ ] **Task 7.3 — Add GetContextResponse type** (5 min CC)
  - Append `GetContextResponse` interface to `mcp-server/src/types.ts` per the spec block above.
  - Run `npm run build` — fix any inferred-type errors in the handler return.

- [ ] **Task 7.4 — Write get-context-tool.test.ts** (15 min CC)
  - Create `mcp-server/tests/get-context-tool.test.ts` covering Cases 1–9 from the table above.
  - Mirror the seed pattern from `list-concepts-tool.test.ts` (makeVault + minimal schema seed). get_context's SQLite reader hits `docs`, `concepts`, `edges` — seed all three tables. Use `resetVerboseForTesting` from `protocol/index.ts` in `beforeEach` to clear the rate-limit tracker.
  - Run `npm test -- --testPathPatterns=get-context-tool`. Iterate until green.

- [ ] **Task 7.5 — CHANGELOG + audit doc** (10 min CC)
  - Append CHANGELOG entry per the spec block above.
  - Generate audit doc: `cd mcp-server && npm run audit -- --vault <local vault>` (or note unavailable + skip).
  - Cross-link the audit doc from the spec's "Concrete sub-PR ordering" checklist (mark PR 7 done).

- [ ] **Task 7.6 — Code-review pass** (10 min CC)
  - Re-read the diff with the project's `/review` skill. Apply auto-fixes.
  - Run full `npm test` — expect 340 + N new tests green.

- [ ] **Task 7.7 — Commit + push + open PR** (5 min CC)
  - Single squash-friendly commit. Title: `feat(mcp-server): get_context reason-string verbose (#50 PR 7)`.
  - Body summarizes behavior change, cites spec, lists tests added.
  - `gh pr create` referencing #50.

---

## Eng-review checklist (for /plan-eng-review subagent)

- [ ] Soft-downgrade semantics matches spec line 373–376 (downgrade vs error).
- [ ] `verbose: true` (boolean) returns INVALID_ARG (parseVerbose handles this — verify).
- [ ] `verbose` on non-"full" depths: validated for type, ignored semantically. Confirmed in Task 7.4 Case 5.
- [ ] `verboseNote` field can coexist with `syncWarning` (both attached to the response object, no clobbering). Confirmed in Task 7.4 Case 8.
- [ ] No cursor protocol primitives invoked (`canonicalizeQueryHash`, `decodeCursor`, `checkRefusal`, `recordIssued`, `issueCursor`). get_context does NOT paginate.
- [ ] `activeOwner` env resolution: matches `search_memory` (`SCHIST_AGENT_ID ?? ""`) — NOT the NAME-first chain that list_concepts/list_domains use. Rationale: `get_context` is memory-DB-adjacent (vault-level read) but conceptually a session-start tool; aligning with search_memory keeps the verbose-rate-limit bucket consistent with the other verbose adopter. **Confirm with eng-review** whether this is correct or whether get_context should use the NAME chain. If revisited, see #115.
- [ ] Rate-limit window: existing `VERBOSE_RATE_LIMIT_PER_MIN = 30` / `VERBOSE_RATE_LIMIT_WINDOW_MS = 60_000` (per `protocol/verbose.ts:4-5`). PR 7 inherits these — no per-tool override needed.
- [ ] Logging side effect: `logVerbose` writes to stderr. Test harness should not depend on stderr capture; verify via `verbose-tool` calls in tests (already pattern-established by `search_memory` tests).

---

## Risk assessment

| Risk | Probability | Mitigation |
|---|---|---|
| Behavior change breaks downstream callers that rely on `depth: "full"` returning `tagCloud` | Medium | CHANGELOG entry + verboseNote hint give callers a clear migration path. No HARD error. |
| Misclassifying boolean `verbose: true` as INVALID_ARG when callers (e.g. old `verbose: true` boolean from PR-1-era code) still pass it | Low | parseVerbose has been live across all 5 cursor adopters; pattern is established. PR 7 is the LAST adopter — if boolean verbose were still in use anywhere, search_memory/search_notes would have caught it already. |
| Rate-limit tracker bleeds across tests | Medium | `resetVerboseForTesting` exists for this. `beforeEach` clears it. |
| Audit doc reproduction depends on a real vault | Low | Note in audit doc that synthetic-fixture measurement is acceptable; PR 8 (docs) will fold the canonical post-rollout numbers. |

---

## Out of scope

- **NOT pagination.** get_context returns a fixed-shape summary; spec §"cursor-input table" excludes it.
- **NOT writing a vault.yaml participant for the verbose audit log.** logVerbose writes to stderr; no DB writes.
- **NOT touching the SQLite reader `getContext`.** PR 7 is purely a handler-level gate. `sqliteReader.getContext` already accepts the three depth tiers.
- **NOT addressing #115** (activeOwner env-chain unification). get_context will use `SCHIST_AGENT_ID ?? ""` matching `search_memory`; the broader cleanup waits.
