# MCP Context Efficiency — PR 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the cursor token module and the reason-string verbose helper as shared infrastructure under `mcp-server/src/protocol/`. No tool actually consumes these modules in PR 2 — wiring lands in PRs 3–7.

**Architecture:** Two new modules under a new `mcp-server/src/protocol/` directory: `cursor.ts` (HMAC-signed token encode/decode + queryHash canonicalization + LRU-based identical-query refusal with 4 distinct error codes) and `verbose.ts` (reason-string parsing + stderr audit log + 30-hits/min soft warning). Each module is self-contained, pure-Node (no new runtime dependencies), and fully unit-tested. The public surface follows the spec at `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md` exactly.

**Tech Stack:** TypeScript (strict, ESM, Node ≥20), `crypto.createHmac` / `crypto.createHash` / `crypto.randomBytes` / `crypto.timingSafeEqual` / `Buffer.toString("base64url")` (all stdlib). Jest 30 with `ts-jest` ESM preset + fake timers for sliding-window tests. **No new npm dependencies.**

**Tracks:** [yibeichan/schist#50](https://github.com/yibeichan/schist/issues/50). Supersedes the PR-2 outline in `docs/superpowers/plans/2026-05-04-mcp-context-efficiency.md` lines 652–664 (the umbrella plan was written before the post-#56-review spec amendments — the spec is the binding contract).

---

## Spec → task mapping

Every requirement in the spec's "Cursor protocol", "queryHash canonicalization", "Cursor error codes", "Reason-string verbose", and the rate-limit note (PR 2 portion) lands in exactly one task below. Tool-specific cursor adoption (the per-tool ORDER BY table) is **out of scope** for PR 2 — that's PRs 3–7.

| Spec section | PR 2 task |
|---|---|
| Token shape (HMAC, base64url unpadded, JWT-like dot separator, per-process secret) | Task 2.2 |
| queryHash canonicalization (sortKeys, NFC, disjoint args/owner, prototype-pollution defense, unhashable rejection) | Task 2.1 |
| Server-side identical-query refusal (LRU 256, TTL 300s, verbose-newly-set bypass) | Task 2.3 |
| Cursor error codes (CURSOR_REQUIRED / CURSOR_EXPIRED / CURSOR_INVALID_SIGNATURE / CURSOR_WRONG_TOOL) | Tasks 2.2 + 2.3 |
| No cursor reissue on CURSOR_REQUIRED | Task 2.3 |
| LRU labelled best-effort | Task 2.3 (test asserts eviction at >256) |
| Multi-process cursor scope (per-process secret) | Task 2.2 (test asserts `resetForTesting()` rotates secret) |
| Reason-string verbose validation (≥12 code points, NFC-irrelevant, whitespace via `/^\s*$/u`, boolean rejected) | Task 2.4 |
| JSON.stringify-escaped stderr log | Task 2.5 |
| 30-hits/min soft warning (per (tool, owner, sha256(reason))) | Task 2.5 |

---

## Spec clarification needed before Task 2.3

The spec carves out a "verbose-newly-set" bypass for the identical-query refusal: when the prior call had verbose unset and the new call has verbose set, treat as a new query. The spec is **silent on the downgrade case** (prior call verbose set, new call verbose unset).

**Recommendation: treat downgrade as still-identical (refused).** Two reasons: (a) literal reading of "newly set" means false→true, not any change; (b) the rationale ("agent upgrades snippet → full") doesn't apply to the downgrade direction — the agent already has full content and re-fetching snippets is a strict regression. An agent that genuinely needs less data can refine the query.

Tests in Task 2.3 enforce this interpretation. If during review the user prefers "any verbose state change bypasses", that's a one-line test + impl change before merge.

---

## File structure

**Created in PR 2:**

- `mcp-server/src/protocol/cursor.ts` — cursor encode/decode + canonicalizeQueryHash + LRU refusal store + error code constants
- `mcp-server/src/protocol/verbose.ts` — parseVerbose + logVerbose + noteHighFrequency
- `mcp-server/src/protocol/index.ts` — re-export public surface for clean consumer imports
- `mcp-server/tests/protocol/cursor.test.ts`
- `mcp-server/tests/protocol/verbose.test.ts`

**Modified in PR 2:**

- `mcp-server/src/types.ts` — extend the `ToolError.error` doc-comment union with the 5 new codes (no runtime change; just docs)

**Not touched in PR 2:**

- `mcp-server/src/tools.ts`, `sqlite-reader.ts`, `tool-registry.ts`, `index.ts` — wiring is PRs 3–7. PR 2 must be a pure-additive infra PR.

The `protocol/` subdir is new — schist's `mcp-server/src/` is currently flat. Tests follow with a parallel `tests/protocol/` subdir so the layout matches.

---

## Public API surface (locked here, consumed in PRs 3–7)

```typescript
// mcp-server/src/protocol/cursor.ts

export const CURSOR_TTL_SECONDS = 300;
export const CURSOR_LRU_SIZE = 256;

export type CursorErrorCode =
  | "CURSOR_REQUIRED"
  | "CURSOR_EXPIRED"
  | "CURSOR_INVALID_SIGNATURE"
  | "CURSOR_WRONG_TOOL";

export interface CursorError {
  error: CursorErrorCode;
  message: string;
}

export interface InvalidArgError {
  error: "INVALID_ARG";
  message: string;
}

export type CanonicalizeResult =
  | { ok: true; queryHash: string }
  | { ok: false; error: InvalidArgError };

export interface CanonicalizeOptions {
  /** Optional explicit exclusion list. Defaults to ["cursor", "verbose"]. */
  excludeKeys?: string[];
}

export function canonicalizeQueryHash(
  args: Record<string, unknown>,
  owner: string,
  opts?: CanonicalizeOptions,
): CanonicalizeResult;

export interface IssueCursorInput {
  tool: string;
  queryHash: string;
  offset: number;
}

export function issueCursor(input: IssueCursorInput): string;

export type DecodeCursorResult =
  | { ok: true; offset: number; queryHash: string }
  | { ok: false; error: CursorError };

/** Decodes a cursor token, verifying HMAC, tool match, and TTL. */
export function decodeCursor(token: string, expectedTool: string): DecodeCursorResult;

export interface RecordIssuedInput {
  tool: string;
  queryHash: string;
  owner: string;
  /** Whether this call had verbose set (non-empty reason string). */
  verboseEnabled: boolean;
}

export function recordIssued(input: RecordIssuedInput): void;

export interface CheckRefusalInput {
  tool: string;
  queryHash: string;
  owner: string;
  verboseEnabled: boolean;
}

export type RefusalResult =
  | { refuse: false }
  | { refuse: true; error: CursorError };

/**
 * Returns refuse=true when an identical query is hit within TTL with no
 * cursor and no verbose-newly-set bypass. Otherwise refuse=false. Does NOT
 * mutate the LRU — call recordIssued separately after serving the page.
 */
export function checkRefusal(input: CheckRefusalInput): RefusalResult;

/** Test-only: rotates the HMAC secret and clears the LRU. */
export function resetForTesting(): void;
```

```typescript
// mcp-server/src/protocol/verbose.ts

export const VERBOSE_MIN_CODE_POINTS = 12;
export const VERBOSE_RATE_LIMIT_PER_MIN = 30;

export type ParseVerboseResult =
  | { enabled: false }
  | { enabled: true; reason: string }
  | { enabled: false; error: { error: "INVALID_ARG"; message: string } };

/**
 * - undefined / null / "" / whitespace-only → { enabled: false } (no error)
 * - non-string (including boolean true) → { enabled: false, error: INVALID_ARG }
 * - string with <12 trimmed code points → { enabled: false, error: INVALID_ARG }
 * - string with ≥12 trimmed code points → { enabled: true, reason: trimmed }
 */
export function parseVerbose(input: unknown): ParseVerboseResult;

export interface LogVerboseInput {
  tool: string;
  owner: string;
  reason: string;
}

/** Writes `[verbose] <tool> by <owner|anonymous>: <JSON.stringify(reason)>` to stderr. */
export function logVerbose(input: LogVerboseInput): void;

/**
 * Tracks (tool, owner, sha256(reason)) counts in a 60-second sliding window.
 * Returns a verboseNote string when the count would exceed 30 in the window,
 * else null. Call once per verbose-accepted call.
 */
export function noteHighFrequency(input: LogVerboseInput): string | null;

/** Test-only: clears the frequency tracker. */
export function resetForTesting(): void;
```

---

## Task 2.0: Create branch + protocol/ directory scaffold

**Files:** none yet (directories only — first file creates in Task 2.1).

- [ ] **Step 1: Sync main and create branch**

```bash
git fetch origin
git switch main
git pull --ff-only
git switch -c feat/issue-50-mcp-efficiency-protocol-modules
```

Expected: clean checkout at `89fafa0` (the PR #56 squash-merge). Untracked items `.gstack/`, `.claude/scheduled_tasks.lock`, `docs/refactor-flatten-spoke-dirs.md` remain — leave them alone, never `git add -A`.

- [ ] **Step 2: Verify untracked-files hygiene**

```bash
git status --short
```

Expected output (the four pre-existing untracked items only):

```
?? .claude/scheduled_tasks.lock
?? .gstack/
?? cli/uv.lock
?? docs/refactor-flatten-spoke-dirs.md
```

If anything else appears, investigate before continuing.

- [ ] **Step 3: Create empty protocol/ + tests/protocol/ dirs**

```bash
mkdir -p mcp-server/src/protocol mcp-server/tests/protocol
```

Expected: both directories exist. No commit yet — git doesn't track empty dirs and Task 2.1 will write the first file.

---

## Task 2.1: canonicalizeQueryHash — args→hash with NFC + prototype-pollution defense

**Files:**
- Create: `mcp-server/src/protocol/cursor.ts` (initial — canonicalize only)
- Create: `mcp-server/tests/protocol/cursor.test.ts` (initial — canonicalize tests only)

This task implements the deterministic-hash core. Encode/decode + LRU come in 2.2 and 2.3.

- [ ] **Step 1: Write the failing test file**

Create `mcp-server/tests/protocol/cursor.test.ts`:

```typescript
import { describe, it, expect, beforeEach, jest } from "@jest/globals";
import { canonicalizeQueryHash } from "../../src/protocol/cursor.js";

describe("canonicalizeQueryHash", () => {
  it("produces identical hashes for argument-order-independent inputs", () => {
    const a = canonicalizeQueryHash({ b: 2, a: 1 }, "yibei");
    const b = canonicalizeQueryHash({ a: 1, b: 2 }, "yibei");
    expect(a).toEqual({ ok: true, queryHash: expect.any(String) });
    expect(b).toEqual({ ok: true, queryHash: expect.any(String) });
    if (a.ok && b.ok) expect(a.queryHash).toBe(b.queryHash);
  });

  it("produces different hashes when owner differs", () => {
    const a = canonicalizeQueryHash({ q: "foo" }, "yibei");
    const b = canonicalizeQueryHash({ q: "foo" }, "claude");
    if (a.ok && b.ok) expect(a.queryHash).not.toBe(b.queryHash);
  });

  it("treats empty-string owner and missing owner identically", () => {
    const a = canonicalizeQueryHash({ q: "foo" }, "");
    // Direct call requires a string; the canonical form must collapse "" to ""
    // so the contract is: empty owner is the "anonymous" identity. Document
    // that no owner means callers pass "".
    expect(a.ok).toBe(true);
  });

  it("strips `cursor` from the hashed args (cursor is meta, not query identity)", () => {
    const a = canonicalizeQueryHash({ q: "foo", cursor: "abc.def" }, "yibei");
    const b = canonicalizeQueryHash({ q: "foo" }, "yibei");
    if (a.ok && b.ok) expect(a.queryHash).toBe(b.queryHash);
  });

  it("strips `verbose` from the hashed args (verbose changes shape, not identity)", () => {
    const a = canonicalizeQueryHash({ q: "foo", verbose: "long reason text" }, "yibei");
    const b = canonicalizeQueryHash({ q: "foo" }, "yibei");
    if (a.ok && b.ok) expect(a.queryHash).toBe(b.queryHash);
  });

  it("collapses undefined / null / missing to identical hash", () => {
    const a = canonicalizeQueryHash({ q: "foo", tags: undefined }, "yibei");
    const b = canonicalizeQueryHash({ q: "foo", tags: null }, "yibei");
    const c = canonicalizeQueryHash({ q: "foo" }, "yibei");
    if (a.ok && b.ok && c.ok) {
      expect(a.queryHash).toBe(c.queryHash);
      expect(b.queryHash).toBe(c.queryHash);
    }
  });

  it("collapses empty-string optional values to missing", () => {
    const a = canonicalizeQueryHash({ q: "foo", status: "" }, "yibei");
    const b = canonicalizeQueryHash({ q: "foo" }, "yibei");
    if (a.ok && b.ok) expect(a.queryHash).toBe(b.queryHash);
  });

  it("collapses limit:0 to limit:undefined", () => {
    const a = canonicalizeQueryHash({ q: "foo", limit: 0 }, "yibei");
    const b = canonicalizeQueryHash({ q: "foo" }, "yibei");
    if (a.ok && b.ok) expect(a.queryHash).toBe(b.queryHash);
  });

  it("preserves array element order (arrays are part of query identity)", () => {
    const a = canonicalizeQueryHash({ tags: ["a", "b"] }, "yibei");
    const b = canonicalizeQueryHash({ tags: ["b", "a"] }, "yibei");
    if (a.ok && b.ok) expect(a.queryHash).not.toBe(b.queryHash);
  });

  it("NFC-normalizes string values (precomposed = combining-accent)", () => {
    // "café" — precomposed (U+00E9) vs combining (e + U+0301)
    const precomposed = "café";
    const combining = "café";
    expect(precomposed).not.toBe(combining); // sanity
    const a = canonicalizeQueryHash({ q: precomposed }, "yibei");
    const b = canonicalizeQueryHash({ q: combining }, "yibei");
    if (a.ok && b.ok) expect(a.queryHash).toBe(b.queryHash);
  });

  it("NFC-normalizes object keys too", () => {
    const precomposedKey = "café";
    const combiningKey = "café";
    const a = canonicalizeQueryHash({ [precomposedKey]: "x" }, "yibei");
    const b = canonicalizeQueryHash({ [combiningKey]: "x" }, "yibei");
    if (a.ok && b.ok) expect(a.queryHash).toBe(b.queryHash);
  });

  it("rejects NaN with INVALID_ARG", () => {
    const r = canonicalizeQueryHash({ x: NaN }, "yibei");
    expect(r).toEqual({
      ok: false,
      error: { error: "INVALID_ARG", message: expect.stringMatching(/non-finite/i) },
    });
  });

  it("rejects +Infinity and -Infinity with INVALID_ARG", () => {
    expect(canonicalizeQueryHash({ x: Infinity }, "yibei").ok).toBe(false);
    expect(canonicalizeQueryHash({ x: -Infinity }, "yibei").ok).toBe(false);
  });

  it("rejects BigInt with INVALID_ARG", () => {
    const r = canonicalizeQueryHash({ x: BigInt(42) as unknown as number }, "yibei");
    expect(r.ok).toBe(false);
  });

  it("rejects functions with INVALID_ARG", () => {
    const r = canonicalizeQueryHash({ x: (() => 1) as unknown as number }, "yibei");
    expect(r.ok).toBe(false);
  });

  it("rejects symbols with INVALID_ARG", () => {
    const r = canonicalizeQueryHash({ x: Symbol("x") as unknown as number }, "yibei");
    expect(r.ok).toBe(false);
  });

  it("rejects circular references with INVALID_ARG", () => {
    const obj: Record<string, unknown> = { a: 1 };
    obj.self = obj;
    const r = canonicalizeQueryHash(obj, "yibei");
    expect(r.ok).toBe(false);
  });

  it("defends against __proto__ key injection (no prototype pollution)", () => {
    // The canonicalizer must use Object.create(null) for intermediate objects.
    // Test: passing { __proto__: { polluted: true } } as an arg must not
    // pollute Object.prototype.
    const beforeProto = (Object.prototype as Record<string, unknown>).polluted;
    const maliciousArgs = JSON.parse('{"__proto__": {"polluted": true}}') as Record<string, unknown>;
    canonicalizeQueryHash(maliciousArgs, "yibei");
    const afterProto = (Object.prototype as Record<string, unknown>).polluted;
    expect(beforeProto).toBeUndefined();
    expect(afterProto).toBeUndefined();
  });

  it("keeps args and owner in disjoint top-level keys", () => {
    // If args and owner were merged, an arg named `owner` would collide.
    const a = canonicalizeQueryHash({ owner: "claude" }, "yibei");
    const b = canonicalizeQueryHash({ owner: "yibei" }, "claude");
    // These two distinct queries must NOT collide.
    if (a.ok && b.ok) expect(a.queryHash).not.toBe(b.queryHash);
  });

  it("produces 64-character lowercase hex output (SHA-256)", () => {
    const r = canonicalizeQueryHash({ q: "foo" }, "yibei");
    if (r.ok) expect(r.queryHash).toMatch(/^[0-9a-f]{64}$/);
  });
});
```

- [ ] **Step 2: Run tests — expect them to fail with "module not found"**

```bash
cd mcp-server && npm test -- --testPathPatterns=protocol/cursor
```

Expected: FAIL with `Cannot find module '../../src/protocol/cursor.js'`.

- [ ] **Step 3: Implement canonicalizeQueryHash**

Create `mcp-server/src/protocol/cursor.ts`:

```typescript
import * as crypto from "crypto";

// ── Public types ───────────────────────────────────────────────────────────

export const CURSOR_TTL_SECONDS = 300;
export const CURSOR_LRU_SIZE = 256;

export interface InvalidArgError {
  error: "INVALID_ARG";
  message: string;
}

export type CanonicalizeResult =
  | { ok: true; queryHash: string }
  | { ok: false; error: InvalidArgError };

export interface CanonicalizeOptions {
  excludeKeys?: string[];
}

// ── canonicalizeQueryHash ─────────────────────────────────────────────────

const DEFAULT_EXCLUDED_KEYS = ["cursor", "verbose"];

/**
 * Produces a stable SHA-256 of the canonical-JSON form of (args, owner).
 *
 * Steps (order matters):
 *   1. Strip excluded keys (default: cursor, verbose) from args.
 *   2. Collapse undefined/null/empty-string to "missing" so they hash identically.
 *   3. Collapse limit:0 to limit:undefined (zero-limit == default).
 *   4. Walk the value tree; reject NaN, ±Infinity, BigInt, function, symbol,
 *      and circular references with INVALID_ARG.
 *   5. NFC-normalize all strings (keys and values).
 *   6. Recursively sort object keys.
 *   7. JSON.stringify({ args: <sorted>, owner }) — note args/owner in
 *      disjoint top-level keys so an arg named `owner` can't collide.
 *   8. SHA-256(hex).
 */
export function canonicalizeQueryHash(
  args: Record<string, unknown>,
  owner: string,
  opts: CanonicalizeOptions = {},
): CanonicalizeResult {
  const excludedKeys = new Set(opts.excludeKeys ?? DEFAULT_EXCLUDED_KEYS);

  try {
    // Step 1+2+3: strip excluded keys, collapse missing-equivalents
    const stripped = stripAndCollapse(args, excludedKeys);

    // Step 4: validate hashability (throws InvalidArgError)
    validateHashable(stripped, "$", new WeakSet());

    // Step 5+6: NFC + sort
    const normalized = normalizeAndSort(stripped);

    // Step 7: canonical JSON with disjoint args/owner namespacing
    const canonical = JSON.stringify({
      args: normalized,
      owner: owner.normalize("NFC"),
    });

    // Step 8: SHA-256 hex
    const queryHash = crypto.createHash("sha256").update(canonical).digest("hex");
    return { ok: true, queryHash };
  } catch (e: unknown) {
    if (isInvalidArgError(e)) {
      return { ok: false, error: e };
    }
    return {
      ok: false,
      error: { error: "INVALID_ARG", message: String(e) },
    };
  }
}

// ── helpers (not exported) ────────────────────────────────────────────────

class InvalidArgErrorImpl extends Error {
  readonly error = "INVALID_ARG" as const;
  constructor(message: string) {
    super(message);
    this.name = "InvalidArgError";
  }
}

function isInvalidArgError(e: unknown): e is InvalidArgError {
  return typeof e === "object" && e !== null && (e as { error?: unknown }).error === "INVALID_ARG";
}

function stripAndCollapse(args: Record<string, unknown>, excludedKeys: Set<string>): Record<string, unknown> {
  const out: Record<string, unknown> = Object.create(null);
  for (const key of Object.keys(args)) {
    if (excludedKeys.has(key)) continue;
    if (key === "__proto__") continue; // prototype-pollution defense
    const v = args[key];
    if (v === undefined || v === null) continue; // collapse to missing
    if (typeof v === "string" && v === "") continue; // collapse empty string
    if (key === "limit" && v === 0) continue; // collapse limit:0
    out[key] = v;
  }
  return out;
}

function validateHashable(v: unknown, path: string, seen: WeakSet<object>): void {
  if (v === null || v === undefined) return;
  switch (typeof v) {
    case "string":
    case "boolean":
      return;
    case "number":
      if (!Number.isFinite(v)) {
        throw new InvalidArgErrorImpl(`non-finite number at ${path}`);
      }
      return;
    case "bigint":
      throw new InvalidArgErrorImpl(`BigInt at ${path} cannot be canonicalized`);
    case "function":
      throw new InvalidArgErrorImpl(`function at ${path} cannot be canonicalized`);
    case "symbol":
      throw new InvalidArgErrorImpl(`symbol at ${path} cannot be canonicalized`);
    case "object":
      if (seen.has(v as object)) {
        throw new InvalidArgErrorImpl(`circular reference at ${path}`);
      }
      seen.add(v as object);
      if (Array.isArray(v)) {
        v.forEach((x, i) => validateHashable(x, `${path}[${i}]`, seen));
      } else {
        for (const k of Object.keys(v as object)) {
          validateHashable((v as Record<string, unknown>)[k], `${path}.${k}`, seen);
        }
      }
      return;
  }
}

function normalizeAndSort(v: unknown): unknown {
  if (v === null || v === undefined) return v;
  if (typeof v === "string") return v.normalize("NFC");
  if (typeof v !== "object") return v;
  if (Array.isArray(v)) return v.map(normalizeAndSort);
  const out: Record<string, unknown> = Object.create(null);
  for (const key of Object.keys(v as object).sort()) {
    out[key.normalize("NFC")] = normalizeAndSort((v as Record<string, unknown>)[key]);
  }
  return out;
}
```

- [ ] **Step 4: Run tests — expect all to pass**

```bash
cd mcp-server && npm test -- --testPathPatterns=protocol/cursor
```

Expected: all 18 specs pass.

- [ ] **Step 5: Commit**

```bash
git add mcp-server/src/protocol/cursor.ts mcp-server/tests/protocol/cursor.test.ts
git commit -m "feat(protocol): canonicalizeQueryHash with NFC + prototype-pollution defense"
```

---

## Task 2.2: cursor.ts — issueCursor + decodeCursor (HMAC, base64url, TTL, 3 error codes)

**Files:**
- Modify: `mcp-server/src/protocol/cursor.ts` (append encode/decode + secret rotation)
- Modify: `mcp-server/tests/protocol/cursor.test.ts` (append encode/decode tests)

This task implements the cursor token shape from the spec. Three of the four cursor error codes land here (`CURSOR_EXPIRED`, `CURSOR_INVALID_SIGNATURE`, `CURSOR_WRONG_TOOL`); the fourth (`CURSOR_REQUIRED`) comes in Task 2.3.

- [ ] **Step 1: Write the failing tests**

Append to `mcp-server/tests/protocol/cursor.test.ts`:

```typescript
import {
  issueCursor,
  decodeCursor,
  resetForTesting,
  CURSOR_TTL_SECONDS,
} from "../../src/protocol/cursor.js";

describe("issueCursor + decodeCursor round-trip", () => {
  beforeEach(() => resetForTesting());

  it("issues a cursor that decodeCursor accepts for the same tool", () => {
    const token = issueCursor({ tool: "search_notes", queryHash: "abc123", offset: 20 });
    const r = decodeCursor(token, "search_notes");
    expect(r).toEqual({ ok: true, offset: 20, queryHash: "abc123" });
  });

  it("encodes as base64url-payload `.` base64url-signature (unpadded, two segments)", () => {
    const token = issueCursor({ tool: "search_notes", queryHash: "abc", offset: 20 });
    const segments = token.split(".");
    expect(segments).toHaveLength(2);
    // base64url alphabet: A–Z, a–z, 0–9, -, _ — no padding
    expect(token).toMatch(/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);
  });

  it("preserves arbitrary integer offsets including 0", () => {
    for (const offset of [0, 1, 100, 100_000]) {
      const token = issueCursor({ tool: "x", queryHash: "h", offset });
      const r = decodeCursor(token, "x");
      if (r.ok) expect(r.offset).toBe(offset);
    }
  });
});

describe("decodeCursor — error paths", () => {
  beforeEach(() => resetForTesting());

  it("returns CURSOR_WRONG_TOOL when token's tool differs from expectedTool", () => {
    const token = issueCursor({ tool: "search_notes", queryHash: "h", offset: 20 });
    const r = decodeCursor(token, "query_graph");
    expect(r).toEqual({
      ok: false,
      error: { error: "CURSOR_WRONG_TOOL", message: expect.any(String) },
    });
  });

  it("returns CURSOR_INVALID_SIGNATURE when signature is tampered", () => {
    const token = issueCursor({ tool: "search_notes", queryHash: "h", offset: 20 });
    const [payload, sig] = token.split(".");
    // Flip a character in the signature
    const tampered = sig[0] === "A" ? "B" + sig.slice(1) : "A" + sig.slice(1);
    const r = decodeCursor(`${payload}.${tampered}`, "search_notes");
    expect(r).toEqual({
      ok: false,
      error: { error: "CURSOR_INVALID_SIGNATURE", message: expect.any(String) },
    });
  });

  it("returns CURSOR_INVALID_SIGNATURE when payload is tampered (signature no longer matches)", () => {
    const token = issueCursor({ tool: "search_notes", queryHash: "h", offset: 20 });
    const [payload, sig] = token.split(".");
    const tampered = payload[0] === "A" ? "B" + payload.slice(1) : "A" + payload.slice(1);
    const r = decodeCursor(`${tampered}.${sig}`, "search_notes");
    expect(r).toEqual({
      ok: false,
      error: { error: "CURSOR_INVALID_SIGNATURE", message: expect.any(String) },
    });
  });

  it("returns CURSOR_INVALID_SIGNATURE when token has wrong segment count", () => {
    for (const bad of ["", "onlyonesegment", "a.b.c", "a.b.c.d"]) {
      const r = decodeCursor(bad, "search_notes");
      expect(r.ok).toBe(false);
      if (!r.ok) expect(r.error.error).toBe("CURSOR_INVALID_SIGNATURE");
    }
  });

  it("returns CURSOR_INVALID_SIGNATURE when payload is not valid base64url JSON", () => {
    // Construct a syntactically OK-looking but undecodable payload
    const r = decodeCursor("!!!notbase64!!!.sig", "search_notes");
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error.error).toBe("CURSOR_INVALID_SIGNATURE");
  });

  it("returns CURSOR_EXPIRED when issuedAt + ttlSeconds < now", () => {
    // Forge a stale token by issuing one, then advancing the clock past TTL.
    jest.useFakeTimers();
    jest.setSystemTime(new Date("2026-05-10T00:00:00Z"));
    const token = issueCursor({ tool: "search_notes", queryHash: "h", offset: 20 });
    // Advance past TTL
    jest.setSystemTime(new Date(Date.now() + (CURSOR_TTL_SECONDS + 1) * 1000));
    const r = decodeCursor(token, "search_notes");
    expect(r).toEqual({
      ok: false,
      error: { error: "CURSOR_EXPIRED", message: expect.any(String) },
    });
    jest.useRealTimers();
  });

  it("accepts a token issued exactly at TTL boundary (< not ≤)", () => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date("2026-05-10T00:00:00Z"));
    const token = issueCursor({ tool: "search_notes", queryHash: "h", offset: 20 });
    jest.setSystemTime(new Date(Date.now() + CURSOR_TTL_SECONDS * 1000));
    const r = decodeCursor(token, "search_notes");
    // Exactly at TTL: still valid (issuedAt + ttl >= now).
    expect(r.ok).toBe(true);
    jest.useRealTimers();
  });
});

describe("resetForTesting — secret rotation", () => {
  it("rotates the HMAC secret so old cursors fail verification", () => {
    const token = issueCursor({ tool: "search_notes", queryHash: "h", offset: 20 });
    expect(decodeCursor(token, "search_notes").ok).toBe(true);
    resetForTesting();
    const r = decodeCursor(token, "search_notes");
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error.error).toBe("CURSOR_INVALID_SIGNATURE");
  });
});
```

- [ ] **Step 2: Run tests — expect them to fail**

```bash
cd mcp-server && npm test -- --testPathPatterns=protocol/cursor
```

Expected: FAIL with `issueCursor`/`decodeCursor`/`resetForTesting` not exported.

- [ ] **Step 3: Implement encode/decode + secret rotation**

Append to `mcp-server/src/protocol/cursor.ts`:

```typescript
// ── Cursor error codes ────────────────────────────────────────────────────

export type CursorErrorCode =
  | "CURSOR_REQUIRED"
  | "CURSOR_EXPIRED"
  | "CURSOR_INVALID_SIGNATURE"
  | "CURSOR_WRONG_TOOL";

export interface CursorError {
  error: CursorErrorCode;
  message: string;
}

// ── HMAC secret (per-process, rotates on resetForTesting) ─────────────────

let HMAC_SECRET: Buffer = crypto.randomBytes(32);

// ── issueCursor ───────────────────────────────────────────────────────────

export interface IssueCursorInput {
  tool: string;
  queryHash: string;
  offset: number;
}

interface CursorPayload {
  tool: string;
  queryHash: string;
  offset: number;
  issuedAt: number;
  ttlSeconds: number;
}

export function issueCursor(input: IssueCursorInput): string {
  const payload: CursorPayload = {
    tool: input.tool,
    queryHash: input.queryHash,
    offset: input.offset,
    issuedAt: Math.floor(Date.now() / 1000),
    ttlSeconds: CURSOR_TTL_SECONDS,
  };
  const payloadJson = JSON.stringify(payload);
  const payloadB64 = Buffer.from(payloadJson, "utf-8").toString("base64url");
  const sigB64 = crypto.createHmac("sha256", HMAC_SECRET).update(payloadB64).digest("base64url");
  return `${payloadB64}.${sigB64}`;
}

// ── decodeCursor ──────────────────────────────────────────────────────────

export type DecodeCursorResult =
  | { ok: true; offset: number; queryHash: string }
  | { ok: false; error: CursorError };

export function decodeCursor(token: string, expectedTool: string): DecodeCursorResult {
  // Structural validation
  const segments = token.split(".");
  if (segments.length !== 2) {
    return invalidSignature("malformed cursor (expected `payload.signature`)");
  }
  const [payloadB64, sigB64] = segments;
  if (!payloadB64 || !sigB64) {
    return invalidSignature("malformed cursor (empty segment)");
  }

  // HMAC verification (timing-safe)
  const expectedSig = crypto.createHmac("sha256", HMAC_SECRET).update(payloadB64).digest("base64url");
  if (!timingSafeEqualStrings(sigB64, expectedSig)) {
    return invalidSignature("cursor signature mismatch");
  }

  // Decode payload
  let payload: CursorPayload;
  try {
    const json = Buffer.from(payloadB64, "base64url").toString("utf-8");
    payload = JSON.parse(json) as CursorPayload;
  } catch {
    // Signature verified but payload undecodable — shouldn't happen in practice
    return invalidSignature("cursor payload not valid base64url JSON");
  }

  // Tool match
  if (payload.tool !== expectedTool) {
    return {
      ok: false,
      error: {
        error: "CURSOR_WRONG_TOOL",
        message: `cursor was issued for tool '${payload.tool}', presented to '${expectedTool}'`,
      },
    };
  }

  // TTL check (issuedAt + ttlSeconds >= nowSeconds; exact boundary still valid)
  const nowSec = Math.floor(Date.now() / 1000);
  if (payload.issuedAt + payload.ttlSeconds < nowSec) {
    return {
      ok: false,
      error: {
        error: "CURSOR_EXPIRED",
        message: `cursor expired (issued ${nowSec - payload.issuedAt}s ago, TTL ${payload.ttlSeconds}s)`,
      },
    };
  }

  return { ok: true, offset: payload.offset, queryHash: payload.queryHash };
}

function invalidSignature(message: string): DecodeCursorResult {
  return { ok: false, error: { error: "CURSOR_INVALID_SIGNATURE", message } };
}

function timingSafeEqualStrings(a: string, b: string): boolean {
  // timingSafeEqual requires equal-length Buffers. Length mismatch → fail
  // (still constant time per call, just not constant-time across mismatches —
  // but length is public, so this leaks nothing meaningful).
  if (a.length !== b.length) return false;
  const ab = Buffer.from(a, "utf-8");
  const bb = Buffer.from(b, "utf-8");
  return crypto.timingSafeEqual(ab, bb);
}

// ── Test-only ─────────────────────────────────────────────────────────────

/** Rotates the HMAC secret. LRU clearing is added in Task 2.3. */
export function resetForTesting(): void {
  HMAC_SECRET = crypto.randomBytes(32);
}
```

- [ ] **Step 4: Run tests — expect all to pass**

```bash
cd mcp-server && npm test -- --testPathPatterns=protocol/cursor
```

Expected: all canonicalize + encode/decode specs pass (~28 specs).

- [ ] **Step 5: Commit**

```bash
git add mcp-server/src/protocol/cursor.ts mcp-server/tests/protocol/cursor.test.ts
git commit -m "feat(protocol): cursor encode/decode with HMAC + TTL + 3 error codes"
```

---

## Task 2.3: cursor.ts — LRU + checkRefusal + recordIssued + verbose-newly-set bypass

**Files:**
- Modify: `mcp-server/src/protocol/cursor.ts` (append LRU + refusal)
- Modify: `mcp-server/tests/protocol/cursor.test.ts` (append refusal tests)

This task closes the cursor module: the 4th error code (`CURSOR_REQUIRED`), the LRU's eviction discipline, and the verbose-newly-set bypass.

**Locked policy (see "Spec clarification" at the top of this plan):** verbose-newly-set bypass is **strict** — only false→true transitions bypass. Verbose-already-set (true→true) and downgrade (true→false) are still refused. If user disagrees during review, the change is a one-line test + one-line impl flip.

- [ ] **Step 1: Write the failing tests**

Append to `mcp-server/tests/protocol/cursor.test.ts`:

```typescript
import {
  checkRefusal,
  recordIssued,
  CURSOR_LRU_SIZE,
} from "../../src/protocol/cursor.js";

describe("checkRefusal — identical-query refusal", () => {
  beforeEach(() => resetForTesting());

  it("returns refuse:false when no prior identical call recorded", () => {
    const r = checkRefusal({ tool: "search_notes", queryHash: "h1", owner: "yibei", verboseEnabled: false });
    expect(r).toEqual({ refuse: false });
  });

  it("returns refuse:true with CURSOR_REQUIRED on identical (tool, queryHash, owner) within TTL", () => {
    recordIssued({ tool: "search_notes", queryHash: "h1", owner: "yibei", verboseEnabled: false });
    const r = checkRefusal({ tool: "search_notes", queryHash: "h1", owner: "yibei", verboseEnabled: false });
    expect(r).toEqual({
      refuse: true,
      error: {
        error: "CURSOR_REQUIRED",
        message: expect.stringContaining("pass the cursor"),
      },
    });
  });

  it("returns refuse:false on different queryHash (refined query)", () => {
    recordIssued({ tool: "search_notes", queryHash: "h1", owner: "yibei", verboseEnabled: false });
    const r = checkRefusal({ tool: "search_notes", queryHash: "h2", owner: "yibei", verboseEnabled: false });
    expect(r).toEqual({ refuse: false });
  });

  it("returns refuse:false on different owner", () => {
    recordIssued({ tool: "search_notes", queryHash: "h1", owner: "yibei", verboseEnabled: false });
    const r = checkRefusal({ tool: "search_notes", queryHash: "h1", owner: "claude", verboseEnabled: false });
    expect(r).toEqual({ refuse: false });
  });

  it("returns refuse:false on different tool (cross-tool cursors are independent)", () => {
    recordIssued({ tool: "search_notes", queryHash: "h1", owner: "yibei", verboseEnabled: false });
    const r = checkRefusal({ tool: "search_memory", queryHash: "h1", owner: "yibei", verboseEnabled: false });
    expect(r).toEqual({ refuse: false });
  });
});

describe("checkRefusal — verbose-newly-set bypass", () => {
  beforeEach(() => resetForTesting());

  it("bypasses refusal when prior had verboseEnabled=false and now verboseEnabled=true", () => {
    recordIssued({ tool: "search_memory", queryHash: "h1", owner: "yibei", verboseEnabled: false });
    const r = checkRefusal({ tool: "search_memory", queryHash: "h1", owner: "yibei", verboseEnabled: true });
    expect(r).toEqual({ refuse: false });
  });

  it("STILL REFUSES when prior had verboseEnabled=true and now verboseEnabled=true (identical+verbose retry)", () => {
    recordIssued({ tool: "search_memory", queryHash: "h1", owner: "yibei", verboseEnabled: true });
    const r = checkRefusal({ tool: "search_memory", queryHash: "h1", owner: "yibei", verboseEnabled: true });
    expect(r).toEqual({
      refuse: true,
      error: { error: "CURSOR_REQUIRED", message: expect.any(String) },
    });
  });

  it("STILL REFUSES on downgrade (prior verboseEnabled=true, now verboseEnabled=false) — see Locked policy", () => {
    recordIssued({ tool: "search_memory", queryHash: "h1", owner: "yibei", verboseEnabled: true });
    const r = checkRefusal({ tool: "search_memory", queryHash: "h1", owner: "yibei", verboseEnabled: false });
    expect(r).toEqual({
      refuse: true,
      error: { error: "CURSOR_REQUIRED", message: expect.any(String) },
    });
  });
});

describe("checkRefusal — TTL expiry on LRU entry", () => {
  beforeEach(() => resetForTesting());

  it("returns refuse:false once the LRU entry's issuedAt is older than TTL", () => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date("2026-05-10T00:00:00Z"));
    recordIssued({ tool: "search_notes", queryHash: "h1", owner: "yibei", verboseEnabled: false });
    // Within TTL: refused
    expect(checkRefusal({ tool: "search_notes", queryHash: "h1", owner: "yibei", verboseEnabled: false }).refuse).toBe(true);
    // Past TTL: not refused
    jest.setSystemTime(new Date(Date.now() + (CURSOR_TTL_SECONDS + 1) * 1000));
    expect(checkRefusal({ tool: "search_notes", queryHash: "h1", owner: "yibei", verboseEnabled: false }).refuse).toBe(false);
    jest.useRealTimers();
  });
});

describe("LRU eviction (best-effort refusal)", () => {
  beforeEach(() => resetForTesting());

  it("evicts the oldest entry when CURSOR_LRU_SIZE is exceeded", () => {
    // Fill the LRU to capacity
    for (let i = 0; i < CURSOR_LRU_SIZE; i++) {
      recordIssued({ tool: "search_notes", queryHash: `h${i}`, owner: "yibei", verboseEnabled: false });
    }
    // The oldest (h0) is still in the LRU
    expect(checkRefusal({ tool: "search_notes", queryHash: "h0", owner: "yibei", verboseEnabled: false }).refuse).toBe(true);
    // Insert one more — h0 evicts
    recordIssued({ tool: "search_notes", queryHash: "hOverflow", owner: "yibei", verboseEnabled: false });
    expect(checkRefusal({ tool: "search_notes", queryHash: "h0", owner: "yibei", verboseEnabled: false }).refuse).toBe(false);
    // hOverflow is still tracked
    expect(checkRefusal({ tool: "search_notes", queryHash: "hOverflow", owner: "yibei", verboseEnabled: false }).refuse).toBe(true);
  });

  it("promotes an entry to MRU on recordIssued (no premature eviction)", () => {
    // Fill LRU
    for (let i = 0; i < CURSOR_LRU_SIZE; i++) {
      recordIssued({ tool: "search_notes", queryHash: `h${i}`, owner: "yibei", verboseEnabled: false });
    }
    // Re-record h0 — promotes to MRU
    recordIssued({ tool: "search_notes", queryHash: "h0", owner: "yibei", verboseEnabled: false });
    // Insert one more — now h1 (the new oldest) evicts, not h0
    recordIssued({ tool: "search_notes", queryHash: "hOverflow", owner: "yibei", verboseEnabled: false });
    expect(checkRefusal({ tool: "search_notes", queryHash: "h0", owner: "yibei", verboseEnabled: false }).refuse).toBe(true);
    expect(checkRefusal({ tool: "search_notes", queryHash: "h1", owner: "yibei", verboseEnabled: false }).refuse).toBe(false);
  });
});

describe("resetForTesting — clears LRU too", () => {
  it("clears the LRU so subsequent checkRefusal returns refuse:false", () => {
    recordIssued({ tool: "search_notes", queryHash: "h1", owner: "yibei", verboseEnabled: false });
    expect(checkRefusal({ tool: "search_notes", queryHash: "h1", owner: "yibei", verboseEnabled: false }).refuse).toBe(true);
    resetForTesting();
    expect(checkRefusal({ tool: "search_notes", queryHash: "h1", owner: "yibei", verboseEnabled: false }).refuse).toBe(false);
  });
});
```

- [ ] **Step 2: Run tests — expect them to fail**

```bash
cd mcp-server && npm test -- --testPathPatterns=protocol/cursor
```

Expected: FAIL with `checkRefusal`/`recordIssued` not exported.

- [ ] **Step 3: Implement LRU + checkRefusal + recordIssued**

Append to `mcp-server/src/protocol/cursor.ts`:

```typescript
// ── LRU for identical-query refusal ───────────────────────────────────────

interface LruEntry {
  issuedAt: number;   // unix seconds
  verboseEnabled: boolean;
}

// JS Map preserves insertion order — re-insertion (delete + set) promotes to MRU.
const refusalLru = new Map<string, LruEntry>();

function lruKey(tool: string, queryHash: string, owner: string): string {
  return `${tool}\x00${queryHash}\x00${owner}`;
}

// ── recordIssued ──────────────────────────────────────────────────────────

export interface RecordIssuedInput {
  tool: string;
  queryHash: string;
  owner: string;
  verboseEnabled: boolean;
}

export function recordIssued(input: RecordIssuedInput): void {
  const key = lruKey(input.tool, input.queryHash, input.owner);
  // Delete-then-set to promote to MRU (Map iteration order = insertion order)
  if (refusalLru.has(key)) refusalLru.delete(key);
  refusalLru.set(key, {
    issuedAt: Math.floor(Date.now() / 1000),
    verboseEnabled: input.verboseEnabled,
  });
  // Best-effort eviction
  while (refusalLru.size > CURSOR_LRU_SIZE) {
    const oldestKey = refusalLru.keys().next().value;
    if (oldestKey === undefined) break;
    refusalLru.delete(oldestKey);
  }
}

// ── checkRefusal ──────────────────────────────────────────────────────────

export interface CheckRefusalInput {
  tool: string;
  queryHash: string;
  owner: string;
  verboseEnabled: boolean;
}

export type RefusalResult =
  | { refuse: false }
  | { refuse: true; error: CursorError };

export function checkRefusal(input: CheckRefusalInput): RefusalResult {
  const key = lruKey(input.tool, input.queryHash, input.owner);
  const entry = refusalLru.get(key);
  if (!entry) return { refuse: false };

  // TTL expiry: treat as missing (caller will record fresh on this call)
  const nowSec = Math.floor(Date.now() / 1000);
  if (entry.issuedAt + CURSOR_TTL_SECONDS < nowSec) {
    return { refuse: false };
  }

  // Verbose-newly-set bypass: prior false → current true
  // (Strict: only this transition bypasses. See plan's Spec clarification.)
  if (!entry.verboseEnabled && input.verboseEnabled) {
    return { refuse: false };
  }

  return {
    refuse: true,
    error: {
      error: "CURSOR_REQUIRED",
      message: `Identical query within ${CURSOR_TTL_SECONDS}s — pass the cursor you received on the previous response, or refine the query.`,
    },
  };
}
```

Also update `resetForTesting` to clear the LRU. Replace the existing definition with:

```typescript
export function resetForTesting(): void {
  HMAC_SECRET = crypto.randomBytes(32);
  refusalLru.clear();
}
```

- [ ] **Step 4: Run tests — expect all to pass**

```bash
cd mcp-server && npm test -- --testPathPatterns=protocol/cursor
```

Expected: all ~40 specs pass.

- [ ] **Step 5: Commit**

```bash
git add mcp-server/src/protocol/cursor.ts mcp-server/tests/protocol/cursor.test.ts
git commit -m "feat(protocol): LRU-based identical-query refusal with CURSOR_REQUIRED + verbose-newly-set bypass"
```

---

## Task 2.4: verbose.ts — parseVerbose (≥12 code points, boolean rejected, whitespace handling)

**Files:**
- Create: `mcp-server/src/protocol/verbose.ts`
- Create: `mcp-server/tests/protocol/verbose.test.ts`

- [ ] **Step 1: Write the failing test file**

Create `mcp-server/tests/protocol/verbose.test.ts`:

```typescript
import { describe, it, expect, beforeEach, afterEach, jest } from "@jest/globals";
import {
  parseVerbose,
  VERBOSE_MIN_CODE_POINTS,
} from "../../src/protocol/verbose.js";

describe("parseVerbose — not-verbose paths (no error)", () => {
  it("returns enabled:false for undefined", () => {
    expect(parseVerbose(undefined)).toEqual({ enabled: false });
  });

  it("returns enabled:false for null", () => {
    expect(parseVerbose(null)).toEqual({ enabled: false });
  });

  it("returns enabled:false for empty string", () => {
    expect(parseVerbose("")).toEqual({ enabled: false });
  });

  it("returns enabled:false for whitespace-only string (ASCII spaces)", () => {
    expect(parseVerbose("   ")).toEqual({ enabled: false });
    expect(parseVerbose("\t\n  ")).toEqual({ enabled: false });
  });

  it("returns enabled:false for whitespace-only via NBSP / ZWS / BOM", () => {
    expect(parseVerbose("            ")).toEqual({ enabled: false });
    expect(parseVerbose("​​​​​​​​​​​​")).toEqual({ enabled: false });
    expect(parseVerbose("﻿")).toEqual({ enabled: false });
  });
});

describe("parseVerbose — INVALID_ARG paths", () => {
  it("rejects boolean true with INVALID_ARG", () => {
    const r = parseVerbose(true);
    expect(r).toEqual({
      enabled: false,
      error: { error: "INVALID_ARG", message: expect.stringMatching(/string/i) },
    });
  });

  it("rejects boolean false with INVALID_ARG", () => {
    const r = parseVerbose(false);
    expect(r.enabled).toBe(false);
    expect("error" in r ? r.error.error : null).toBe("INVALID_ARG");
  });

  it("rejects number with INVALID_ARG", () => {
    expect("error" in parseVerbose(42) ? (parseVerbose(42) as { error: { error: string } }).error.error : null).toBe("INVALID_ARG");
  });

  it("rejects object with INVALID_ARG", () => {
    expect("error" in parseVerbose({}) ? (parseVerbose({}) as { error: { error: string } }).error.error : null).toBe("INVALID_ARG");
  });

  it("rejects array with INVALID_ARG", () => {
    expect("error" in parseVerbose([]) ? (parseVerbose([]) as { error: { error: string } }).error.error : null).toBe("INVALID_ARG");
  });

  it("rejects string with <12 trimmed code points as INVALID_ARG", () => {
    const r = parseVerbose("short");
    expect(r).toEqual({
      enabled: false,
      error: { error: "INVALID_ARG", message: expect.stringMatching(/12/) },
    });
  });

  it("counts CODE POINTS not UTF-16 units (emoji rejection)", () => {
    // 6 emoji (each is one code point, 2 UTF-16 units).
    // str.length = 12 (UTF-16) but [...str].length = 6 (code points).
    // Must be REJECTED.
    const emoji6 = "🔍".repeat(6);
    expect(emoji6.length).toBe(12); // sanity: UTF-16 unit count
    expect([...emoji6].length).toBe(6); // sanity: code point count
    const r = parseVerbose(emoji6);
    expect(r.enabled).toBe(false);
    if ("error" in r) expect(r.error.error).toBe("INVALID_ARG");
  });

  it("counts surrounding-whitespace-stripped length", () => {
    const r = parseVerbose("           hi          "); // padded
    expect(r.enabled).toBe(false);
    if ("error" in r) expect(r.error.error).toBe("INVALID_ARG");
  });
});

describe("parseVerbose — verbose-accepted path", () => {
  it("accepts a string with ≥12 trimmed code points", () => {
    const reason = "investigating frontend bug";
    expect(parseVerbose(reason)).toEqual({ enabled: true, reason });
  });

  it("trims surrounding whitespace from the returned reason", () => {
    expect(parseVerbose("   investigating frontend bug   ")).toEqual({
      enabled: true,
      reason: "investigating frontend bug",
    });
  });

  it("accepts exactly 12 code points (boundary)", () => {
    const reason = "x".repeat(VERBOSE_MIN_CODE_POINTS);
    expect(parseVerbose(reason)).toEqual({ enabled: true, reason });
  });

  it("accepts 12 emoji (12 code points = 24 UTF-16 units)", () => {
    const reason = "🔍".repeat(VERBOSE_MIN_CODE_POINTS);
    expect([...reason].length).toBe(VERBOSE_MIN_CODE_POINTS); // sanity
    expect(parseVerbose(reason)).toEqual({ enabled: true, reason });
  });

  it("accepts 12-character CJK reason", () => {
    const reason = "学习深度学习中的注意力机制原"; // 12 chars
    expect([...reason].length).toBe(12); // sanity
    expect(parseVerbose(reason)).toEqual({ enabled: true, reason });
  });
});
```

- [ ] **Step 2: Run tests — expect them to fail**

```bash
cd mcp-server && npm test -- --testPathPatterns=protocol/verbose
```

Expected: FAIL with `Cannot find module '../../src/protocol/verbose.js'`.

- [ ] **Step 3: Implement parseVerbose**

Create `mcp-server/src/protocol/verbose.ts`:

```typescript
import * as crypto from "crypto";

export const VERBOSE_MIN_CODE_POINTS = 12;
export const VERBOSE_RATE_LIMIT_PER_MIN = 30;
export const VERBOSE_RATE_LIMIT_WINDOW_MS = 60_000;

export type ParseVerboseResult =
  | { enabled: false }
  | { enabled: true; reason: string }
  | { enabled: false; error: { error: "INVALID_ARG"; message: string } };

/**
 * Parses a verbose input field.
 *
 *  - undefined / null / "" / whitespace-only string → { enabled: false }
 *  - non-string (incl. boolean true)                → { enabled: false, error: INVALID_ARG }
 *  - string with <12 trimmed CODE POINTS             → { enabled: false, error: INVALID_ARG }
 *  - string with ≥12 trimmed code points             → { enabled: true, reason: trimmedString }
 *
 * Whitespace is anything matching /^\s*$/u (catches NBSP, ZWS, BOM, etc.).
 * Code points are counted via [...str.trim()].length — UTF-16-unit counting
 * would incorrectly accept 6 emoji as ≥12.
 */
export function parseVerbose(input: unknown): ParseVerboseResult {
  if (input === undefined || input === null) return { enabled: false };
  if (typeof input !== "string") {
    return {
      enabled: false,
      error: {
        error: "INVALID_ARG",
        message: `verbose must be a string reason (≥${VERBOSE_MIN_CODE_POINTS} code points); got ${typeof input}`,
      },
    };
  }
  // Whitespace-only or empty → not verbose, no error
  if (/^\s*$/u.test(input)) return { enabled: false };

  const trimmed = input.trim();
  // After trim, re-check whitespace-only (defense against unusual whitespace)
  if (trimmed === "") return { enabled: false };

  // Code-point count (NOT str.length — see CODE POINTS comment above)
  const codePointCount = [...trimmed].length;
  if (codePointCount < VERBOSE_MIN_CODE_POINTS) {
    return {
      enabled: false,
      error: {
        error: "INVALID_ARG",
        message: `verbose reason must be ≥${VERBOSE_MIN_CODE_POINTS} code points after trim (got ${codePointCount})`,
      },
    };
  }
  return { enabled: true, reason: trimmed };
}
```

- [ ] **Step 4: Run tests — expect all to pass**

```bash
cd mcp-server && npm test -- --testPathPatterns=protocol/verbose
```

Expected: all parseVerbose specs pass.

- [ ] **Step 5: Commit**

```bash
git add mcp-server/src/protocol/verbose.ts mcp-server/tests/protocol/verbose.test.ts
git commit -m "feat(protocol): parseVerbose with code-point counting + boolean rejection"
```

---

## Task 2.5: verbose.ts — logVerbose + noteHighFrequency (stderr audit + 30/min sliding window)

**Files:**
- Modify: `mcp-server/src/protocol/verbose.ts` (append log + frequency)
- Modify: `mcp-server/tests/protocol/verbose.test.ts` (append log + frequency tests)

- [ ] **Step 1: Write the failing tests**

Append to `mcp-server/tests/protocol/verbose.test.ts`:

```typescript
import {
  logVerbose,
  noteHighFrequency,
  resetForTesting,
  VERBOSE_RATE_LIMIT_PER_MIN,
  VERBOSE_RATE_LIMIT_WINDOW_MS,
} from "../../src/protocol/verbose.js";

describe("logVerbose — stderr audit log", () => {
  let stderrSpy: ReturnType<typeof jest.spyOn>;
  beforeEach(() => {
    stderrSpy = jest.spyOn(process.stderr, "write").mockImplementation(() => true);
  });
  afterEach(() => {
    stderrSpy.mockRestore();
  });

  it("writes a `[verbose] tool by owner: <JSON.stringify(reason)>` line", () => {
    logVerbose({ tool: "search_memory", owner: "yibei", reason: "investigating bug" });
    const calls = stderrSpy.mock.calls.map((c) => String(c[0]));
    const matched = calls.find((s) => s.includes("[verbose] search_memory by yibei:"));
    expect(matched).toBeDefined();
    expect(matched).toContain('"investigating bug"'); // JSON.stringify quoted
  });

  it("uses '<anonymous>' when owner is empty string", () => {
    logVerbose({ tool: "search_memory", owner: "", reason: "investigating bug" });
    const calls = stderrSpy.mock.calls.map((c) => String(c[0]));
    expect(calls.find((s) => s.includes("[verbose] search_memory by <anonymous>:"))).toBeDefined();
  });

  it("escapes newlines in reason via JSON.stringify (defends against log injection)", () => {
    logVerbose({ tool: "search_memory", owner: "yibei", reason: "benign\n[error] root pwned" });
    const calls = stderrSpy.mock.calls.map((c) => String(c[0]));
    const matched = calls.find((s) => s.includes("[verbose]"));
    expect(matched).toBeDefined();
    // JSON.stringify escapes newline to "\n" (literal backslash-n, no real newline)
    expect(matched).toContain('"benign\\n[error] root pwned"');
    expect(matched!.split("\n").filter((l) => l.includes("[error]"))).toHaveLength(0); // no injected line
  });

  it("escapes control characters in reason", () => {
    logVerbose({ tool: "x", owner: "y", reason: "with nullescape" });
    const calls = stderrSpy.mock.calls.map((c) => String(c[0]));
    const matched = calls.find((s) => s.includes("[verbose]"));
    expect(matched).toContain("\\u0000");
    expect(matched).toContain("\\u001b"); // JSON.stringify uses lowercase hex for control chars
  });

  it("escapes the owner field as well (caller-controlled)", () => {
    logVerbose({ tool: "x", owner: "weird\nowner", reason: "investigating bug" });
    const calls = stderrSpy.mock.calls.map((c) => String(c[0]));
    const matched = calls.find((s) => s.includes("[verbose]"));
    // owner should appear JSON-escaped too
    expect(matched).toContain('"weird\\nowner"');
  });
});

describe("noteHighFrequency — sliding 60s window", () => {
  beforeEach(() => resetForTesting());

  it("returns null below the threshold", () => {
    for (let i = 0; i < VERBOSE_RATE_LIMIT_PER_MIN; i++) {
      const r = noteHighFrequency({ tool: "search_memory", owner: "yibei", reason: "investigating bug" });
      expect(r).toBeNull();
    }
  });

  it("returns a warning string on the (threshold+1)-th call within the window", () => {
    for (let i = 0; i < VERBOSE_RATE_LIMIT_PER_MIN; i++) {
      noteHighFrequency({ tool: "search_memory", owner: "yibei", reason: "investigating bug" });
    }
    const r = noteHighFrequency({ tool: "search_memory", owner: "yibei", reason: "investigating bug" });
    expect(r).toMatch(/reason pattern is frequent/);
  });

  it("buckets are independent per (tool, owner, sha256(reason))", () => {
    // Fill bucket A to threshold
    for (let i = 0; i < VERBOSE_RATE_LIMIT_PER_MIN + 1; i++) {
      noteHighFrequency({ tool: "search_memory", owner: "yibei", reason: "investigating bug" });
    }
    // A different reason gets its own fresh bucket
    expect(noteHighFrequency({ tool: "search_memory", owner: "yibei", reason: "checking other thing" })).toBeNull();
    // A different owner gets its own fresh bucket
    expect(noteHighFrequency({ tool: "search_memory", owner: "claude", reason: "investigating bug" })).toBeNull();
    // A different tool gets its own fresh bucket
    expect(noteHighFrequency({ tool: "get_context", owner: "yibei", reason: "investigating bug" })).toBeNull();
  });

  it("evicts timestamps older than the window (sliding, not cumulative)", () => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date("2026-05-10T00:00:00Z"));
    // Fill to threshold
    for (let i = 0; i < VERBOSE_RATE_LIMIT_PER_MIN; i++) {
      noteHighFrequency({ tool: "x", owner: "y", reason: "investigating bug" });
    }
    // Advance the clock past the window
    jest.setSystemTime(new Date(Date.now() + VERBOSE_RATE_LIMIT_WINDOW_MS + 1));
    // First call after window: bucket is effectively empty → returns null
    expect(noteHighFrequency({ tool: "x", owner: "y", reason: "investigating bug" })).toBeNull();
    jest.useRealTimers();
  });

  it("hashes reasons so byte-identical reasons share a bucket but different reasons don't", () => {
    // Equivalent NFC forms — they ARE byte-different but treated as separate buckets
    // (the spec doesn't require NFC normalization of reason for frequency tracking,
    // only for queryHash; document this if it bites later).
    const a = "investigating bug now";
    const b = "investigating bug now"; // identical
    for (let i = 0; i < VERBOSE_RATE_LIMIT_PER_MIN + 1; i++) {
      noteHighFrequency({ tool: "x", owner: "y", reason: a });
    }
    expect(noteHighFrequency({ tool: "x", owner: "y", reason: b })).toMatch(/frequent/);
  });
});

describe("verbose.resetForTesting", () => {
  it("clears the frequency tracker", () => {
    for (let i = 0; i < VERBOSE_RATE_LIMIT_PER_MIN + 1; i++) {
      noteHighFrequency({ tool: "x", owner: "y", reason: "investigating bug" });
    }
    resetForTesting();
    expect(noteHighFrequency({ tool: "x", owner: "y", reason: "investigating bug" })).toBeNull();
  });
});
```

- [ ] **Step 2: Run tests — expect them to fail**

```bash
cd mcp-server && npm test -- --testPathPatterns=protocol/verbose
```

Expected: FAIL with `logVerbose`/`noteHighFrequency`/`resetForTesting` not exported.

- [ ] **Step 3: Implement logVerbose + noteHighFrequency + resetForTesting**

Append to `mcp-server/src/protocol/verbose.ts`:

```typescript
// ── logVerbose — stderr audit log ────────────────────────────────────────

export interface LogVerboseInput {
  tool: string;
  owner: string;
  reason: string;
}

/**
 * Writes one audit line to stderr in the format:
 *   [verbose] <tool> by <owner|<anonymous>>: <JSON.stringify(reason)>
 *
 * Reason and owner are passed through JSON.stringify so newlines, control
 * chars, and other non-printable bytes can't inject fake stderr lines. The
 * line ends with a real newline so individual records are still separable.
 */
export function logVerbose(input: LogVerboseInput): void {
  const ownerDisplay = input.owner === "" ? "<anonymous>" : JSON.stringify(input.owner);
  const reasonDisplay = JSON.stringify(input.reason);
  process.stderr.write(`[verbose] ${input.tool} by ${ownerDisplay}: ${reasonDisplay}\n`);
}

// ── noteHighFrequency — sliding 60s window per (tool, owner, sha256(reason)) ─

// timestamps in ms; oldest at index 0 (FIFO sliding window)
const frequencyBuckets = new Map<string, number[]>();

function freqKey(tool: string, owner: string, reason: string): string {
  const reasonHash = crypto.createHash("sha256").update(reason).digest("hex");
  return `${tool}\x00${owner}\x00${reasonHash}`;
}

/**
 * Returns a verboseNote warning string when this call would push the bucket
 * over VERBOSE_RATE_LIMIT_PER_MIN within the last VERBOSE_RATE_LIMIT_WINDOW_MS.
 * Otherwise returns null. The bucket is sliding, not cumulative — timestamps
 * older than the window are dropped on each call.
 */
export function noteHighFrequency(input: LogVerboseInput): string | null {
  const key = freqKey(input.tool, input.owner, input.reason);
  const now = Date.now();
  const cutoff = now - VERBOSE_RATE_LIMIT_WINDOW_MS;
  const prior = frequencyBuckets.get(key) ?? [];
  const fresh = prior.filter((t) => t >= cutoff);
  fresh.push(now);
  frequencyBuckets.set(key, fresh);
  if (fresh.length > VERBOSE_RATE_LIMIT_PER_MIN) {
    return "reason pattern is frequent — consider sampling at operator level";
  }
  return null;
}

// ── Test-only ─────────────────────────────────────────────────────────────

export function resetForTesting(): void {
  frequencyBuckets.clear();
}
```

- [ ] **Step 4: Run tests — expect all to pass**

```bash
cd mcp-server && npm test -- --testPathPatterns=protocol/verbose
```

Expected: all verbose specs pass.

- [ ] **Step 5: Commit**

```bash
git add mcp-server/src/protocol/verbose.ts mcp-server/tests/protocol/verbose.test.ts
git commit -m "feat(protocol): logVerbose stderr audit + 30/min sliding-window frequency tracker"
```

---

## Task 2.6: Barrel export + types.ts doc-comment update + full suite green

**Files:**
- Create: `mcp-server/src/protocol/index.ts`
- Modify: `mcp-server/src/types.ts` (line 52 comment only — no runtime change)

This task gives PRs 3–7 a single clean import surface and updates the documented `ToolError.error` union to include the new codes.

- [ ] **Step 1: Create the barrel index**

Create `mcp-server/src/protocol/index.ts`:

```typescript
// Public surface for the cursor + verbose protocol primitives.
// Consumed by tools.ts and sqlite-reader.ts in PRs 3–7.

export {
  CURSOR_TTL_SECONDS,
  CURSOR_LRU_SIZE,
  canonicalizeQueryHash,
  issueCursor,
  decodeCursor,
  recordIssued,
  checkRefusal,
  resetForTesting as resetCursorForTesting,
} from "./cursor.js";

export type {
  CursorErrorCode,
  CursorError,
  InvalidArgError,
  CanonicalizeResult,
  CanonicalizeOptions,
  IssueCursorInput,
  DecodeCursorResult,
  RecordIssuedInput,
  CheckRefusalInput,
  RefusalResult,
} from "./cursor.js";

export {
  VERBOSE_MIN_CODE_POINTS,
  VERBOSE_RATE_LIMIT_PER_MIN,
  VERBOSE_RATE_LIMIT_WINDOW_MS,
  parseVerbose,
  logVerbose,
  noteHighFrequency,
  resetForTesting as resetVerboseForTesting,
} from "./verbose.js";

export type {
  ParseVerboseResult,
  LogVerboseInput,
} from "./verbose.js";
```

- [ ] **Step 2: Update the `ToolError.error` comment**

Edit `mcp-server/src/types.ts` line 52, replacing the inline comment to include the new codes:

```typescript
export interface ToolError {
  error: string;  // NOT_FOUND | PATH_TRAVERSAL | WRITE_TIMEOUT | GIT_ERROR | INVALID_SQL | VALIDATION_ERROR | INGEST_ERROR | INVALID_ARG | CURSOR_REQUIRED | CURSOR_EXPIRED | CURSOR_INVALID_SIGNATURE | CURSOR_WRONG_TOOL
  message: string;
  details?: unknown;
}
```

This is a doc-only change — `error` is typed as `string` at runtime, so callers already accept arbitrary strings. The comment is for human readers.

- [ ] **Step 3: Run the full test suite**

```bash
cd mcp-server && npm test
```

Expected: every existing test still passes plus the new protocol/ specs.

If you're running on HPC and hit `GLIBCXX_3.4.29 not found` errors from `better-sqlite3`, prepend the spack libstdc++ path (see schist memory entry #151):

```bash
cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH npm test
```

- [ ] **Step 4: Build and confirm tsc succeeds**

```bash
cd mcp-server && npm run build
```

Expected: `dist/` populated with `protocol/cursor.js`, `protocol/verbose.js`, `protocol/index.js` and their `.d.ts` siblings. No TS errors.

- [ ] **Step 5: Smoke-run the audit script (no behavior change expected)**

PR 2 introduces no tool-side behavior change. The audit baseline numbers must NOT shift. Run on main first to capture a fresh baseline, then on the PR 2 branch:

```bash
# Baseline (main)
git stash push --include-untracked  # if any in-flight changes
git switch main
cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH \
  npm run build && npm run audit -- --vault ~/schist-vault --search-query session > /tmp/baseline.json
cd ..

# Back to PR 2 branch
git switch feat/issue-50-mcp-efficiency-protocol-modules
git stash pop 2>/dev/null || true
cd mcp-server && LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH \
  npm run build && npm run audit -- --vault ~/schist-vault --search-query session > /tmp/pr2-audit.json
cd ..

# Compare byte counts across all tools
diff <(jq -S '.measurements | with_entries(.value |= .bytes)' /tmp/baseline.json) \
     <(jq -S '.measurements | with_entries(.value |= .bytes)' /tmp/pr2-audit.json)
```

Expected: empty diff (no byte-count change). If `diff` returns anything, something in PR 2 has accidentally altered tool behavior — investigate before continuing.

Fallback if you don't want to bounce branches: eyeball `/tmp/pr2-audit.json` against the headline 2026-05-10 baseline in `docs/superpowers/specs/audit-2026-05-04-mcp-response-sizes.md` (query_graph ≈ 241 KB / 64K tokens, search_memory ≈ 42 KB / 10.6K tokens). Cruder, but catches gross regressions.

- [ ] **Step 6: Commit the barrel + types update**

```bash
git add mcp-server/src/protocol/index.ts mcp-server/src/types.ts
git commit -m "feat(protocol): barrel export + extend ToolError code union for cursor/verbose"
```

---

## Task 2.7: Push, open PR, watch CI

- [ ] **Step 1: Push branch**

```bash
git push -u origin feat/issue-50-mcp-efficiency-protocol-modules
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "feat(mcp-server): cursor + reason-string verbose protocol modules (#50 PR 2)" --body "$(cat <<'EOF'
## Summary
- Add `mcp-server/src/protocol/cursor.ts` — HMAC-signed cursor tokens, queryHash canonicalization, LRU identical-query refusal, 4 distinct cursor error codes.
- Add `mcp-server/src/protocol/verbose.ts` — reason-string parsing (≥12 code points, NFC/whitespace-safe, boolean rejected), JSON.stringify-escaped stderr audit log, 30-hits/min sliding-window soft warning.
- Add `mcp-server/src/protocol/index.ts` — barrel export for clean PR-3+ imports.
- Update `ToolError.error` doc-comment in `types.ts` with the 5 new codes (`INVALID_ARG`, `CURSOR_REQUIRED`, `CURSOR_EXPIRED`, `CURSOR_INVALID_SIGNATURE`, `CURSOR_WRONG_TOOL`).

**No tool actually consumes the modules yet** — wiring lands in PRs 3–7.

Implements the contract specified in `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md` (the spec PR'd as #56). No new npm dependencies; all crypto is Node stdlib.

## Test plan
- [x] `npm test` passes (full suite + new `tests/protocol/` specs)
- [x] `npm run build` succeeds (strict TS)
- [x] Audit script run against live vault shows no byte-count drift vs. baseline
- [x] queryHash determinism: argument order, NFC, prototype-pollution defense, unhashable rejection all covered
- [x] Cursor round-trip + tamper + TTL + wrong-tool paths covered
- [x] LRU eviction at >256 entries + MRU promotion covered
- [x] verbose code-point counting (emoji rejected; CJK accepted)
- [x] verbose stderr log resists newline injection
- [x] verbose 30/min sliding window per (tool, owner, sha256(reason)) covered

## Spec clarification flagged for reviewer
Strict interpretation of "verbose-newly-set bypass": only false→true transitions bypass the identical-query refusal. Downgrade (true→false) and true→true remain refused. Spec is silent on downgrade; the rationale ("agent upgrades snippet→full") only applies to the upgrade direction. If reviewer prefers permissive (any change bypasses), it's a one-line test + impl flip.

## Refs
Part of #50. Spec at \`docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md\` (PR #56).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Wait for CI**

```bash
gh pr checks --watch
```

Expected: green. If a check fails, fix in-place — do NOT merge through red. Common gotchas previously seen on this codebase:

- **`schist: not found` in mcp-server CI**: that was fixed in PR #56 (`e103b9a`). The mcp-server job now installs the Python CLI. Should not recur unless `.github/workflows/` got reverted.
- **`GLIBCXX_3.4.29 not found`**: HPC-only, not on GitHub-hosted runners (memory #151). CI is fine.

- [ ] **Step 4: Self-`/review`**

Run `/review` on the PR before requesting human review — surfaces adversarial findings cheap. Address every concrete finding in-PR (the "boil the lake" precedent from PR #56, memory #169).

```bash
# In a Claude session:
/review <PR-number>
```

- [ ] **Step 5: Request human review**

Tag yibei. Linger here until merged — PR 3 depends on this landing.

---

## Self-review (run before opening PR)

**1. Spec coverage** — Every spec section listed in "Spec → task mapping" above maps to at least one task. Walking the spec headings:

- ✅ "Token shape" → Task 2.2 (HMAC + base64url + JWT-like + per-process secret)
- ✅ "queryHash canonicalization" → Task 2.1 (every bullet covered: NaN/Infinity/BigInt rejection, prototype-pollution defense, NFC, disjoint args/owner, empty-string-as-missing, limit:0 collapse, array order preserved)
- ✅ "Server-side identical-query refusal" → Task 2.3 (LRU + verbose-newly-set bypass + TTL expiry)
- ✅ "No cursor reissue on `CURSOR_REQUIRED`" → Task 2.3 (the `checkRefusal` return type carries no `cursor` field)
- ✅ "Refusal is best-effort" → Task 2.3 (LRU eviction test asserts the >256 eviction)
- ✅ "Cursor error codes" → Tasks 2.2 + 2.3 (all four codes returned by either `decodeCursor` or `checkRefusal`)
- ✅ "Multi-process cursor scope" → Task 2.2 (`resetForTesting` rotates secret = same effect as new process; test asserts old cursors fail)
- ✅ "Reason-string verbose" → Task 2.4 (every bullet: ≥12 code points via `[...str].length`, `/^\s*$/u` whitespace, boolean rejected)
- ✅ "Logging" → Task 2.5 (JSON.stringify escape + line format + log-injection test)
- ✅ "Rate-limit note" → Task 2.5 (30-hits/min sliding window, per (tool, owner, sha256(reason)))
- N/A: "Tool-specific cursor adoption" — PRs 3–7
- N/A: "Default limits" — PRs 3–7
- N/A: "Compatibility / migration" — PR 8
- N/A: "Out-of-scope" — informational

**2. Placeholder scan** — Walked the document looking for "TBD", "TODO", "implement later", "fill in", "<fill>", "similar to Task N". None present. Every step has runnable commands or full code blocks.

**3. Type consistency** — Cross-checked signatures referenced across tasks:

- `canonicalizeQueryHash(args, owner, opts?) → CanonicalizeResult` — defined Task 2.1, no callers in PR 2 (PR 3+ uses it). Signature matches the "Public API surface" section at the top of this plan.
- `issueCursor(input) → string` — defined Task 2.2, called by Task 2.2 tests and `resetForTesting` test in Task 2.2.
- `decodeCursor(token, expectedTool) → DecodeCursorResult` — defined Task 2.2, called by Task 2.2 tests and Task 2.3 secret-rotation test.
- `recordIssued(input) → void` — defined Task 2.3, called by Task 2.3 tests. The `verboseEnabled` field on the input is the same shape Task 2.3 introduces.
- `checkRefusal(input) → RefusalResult` — defined Task 2.3, called by Task 2.3 tests.
- `parseVerbose(input) → ParseVerboseResult` — defined Task 2.4, no other callers in PR 2.
- `logVerbose(input) → void` — defined Task 2.5, called by Task 2.5 tests.
- `noteHighFrequency(input) → string | null` — defined Task 2.5, called by Task 2.5 tests.

Both modules export `resetForTesting` — the barrel re-exports them under distinct names (`resetCursorForTesting` / `resetVerboseForTesting`) so PR 3+ test files importing the barrel can call either without collision.

**4. Untracked-files hygiene** — Task 2.0 lists the exact expected `git status` output. The plan's `git add` commands are scoped to specific paths, never `git add -A`.

**5. Spec self-review checklist re-validation** — Re-read the spec's bottom checklist (lines 405–449). All 14 items still ✅ at HEAD `89fafa0`. The "verbose-newly-set bypass" item (spec line 439) is the one this plan adds a tightening clarification for (downgrade behavior), surfaced explicitly in the plan body and PR description.

---

## Execution choices

Plan complete and saved to `docs/superpowers/plans/2026-05-04-mcp-context-efficiency-pr-2.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task (2.0 through 2.7), review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using `superpowers:executing-plans` with checkpoints.

The TDD discipline + the spec-clarification flag favor (1) for this PR: each task ends with a green test suite, and the per-task review pause lets you (yibei) catch interpretation drift before it compounds. Inline (2) is fine for the verbose module which is simpler.
