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

    // Step 4: validate hashability (throws InvalidArgErrorImpl)
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
      return { ok: false, error: { error: e.error, message: e.message } };
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
    if (Array.isArray(v) && v.length === 0) continue; // collapse empty array — handlers treat [] identically to omitted
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
      try {
        if (Array.isArray(v)) {
          v.forEach((x, i) => validateHashable(x, `${path}[${i}]`, seen));
        } else {
          for (const k of Object.keys(v as object)) {
            validateHashable((v as Record<string, unknown>)[k], `${path}.${k}`, seen);
          }
        }
      } finally {
        seen.delete(v as object);
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
  // Defense in depth: HMAC verification above already rejected anything not
  // signed by this process, so reaching here with non-JSON payload requires
  // either internal corruption or future cursor format changes.
  let payload: CursorPayload;
  try {
    const json = Buffer.from(payloadB64, "base64url").toString("utf-8");
    payload = JSON.parse(json) as CursorPayload;
  } catch {
    // Signature verified but payload undecodable — shouldn't happen in practice
    return invalidSignature("cursor payload not valid base64url JSON");
  }

  // Schema guard: JSON.parse on "null", "[]", or "42" would succeed and yield
  // payload values that crash on property access (`null.tool` → TypeError) or
  // mis-typed (`offset: "10"`, `ttlSeconds: undefined` → NaN-arithmetic). Each
  // field must be the expected type after the HMAC-trusted decode.
  if (
    typeof payload !== "object" ||
    payload === null ||
    Array.isArray(payload) ||
    typeof payload.tool !== "string" ||
    typeof payload.queryHash !== "string" ||
    typeof payload.offset !== "number" ||
    typeof payload.issuedAt !== "number" ||
    typeof payload.ttlSeconds !== "number"
  ) {
    return invalidSignature("cursor payload schema invalid");
  }

  // Tool match
  if (payload.tool !== expectedTool) {
    // Sanitize before interpolation: payload.tool is server-signed, but defense
    // in depth against any future caller passing partly attacker-controlled input
    // to issueCursor. Replace non-[A-Za-z0-9_-] chars with '?' and cap length.
    const safeTool = payload.tool.replace(/[^\w-]/g, "?").slice(0, 64);
    return {
      ok: false,
      error: {
        error: "CURSOR_WRONG_TOOL",
        message: `cursor was issued for tool '${safeTool}', presented to '${expectedTool}'`,
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

// ── LRU for identical-query refusal ───────────────────────────────────────

interface LruEntry {
  issuedAt: number;   // unix seconds
  verboseEnabled: boolean;
}

// JS Map preserves insertion order — re-insertion (delete + set) promotes to MRU.
const refusalLru = new Map<string, LruEntry>();

/**
 * Builds the LRU map key from (tool, queryHash, owner) using NUL bytes as
 * separator. Callers must ensure none of the three inputs contain `\x00`
 * (queryHash is hex from canonicalizeQueryHash, tool is a fixed registry
 * name, owner comes from vault config — all NUL-free in practice).
 */
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
  // Best-effort eviction. Each recordIssued adds at most one entry, so size
  // exceeds CURSOR_LRU_SIZE by at most 1 — `if` is sufficient.
  if (refusalLru.size > CURSOR_LRU_SIZE) {
    const oldestKey = refusalLru.keys().next().value;
    if (oldestKey !== undefined) refusalLru.delete(oldestKey);
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

// ── Test-only ─────────────────────────────────────────────────────────────

/** Rotates the HMAC secret and clears the LRU. */
export function resetForTesting(): void {
  HMAC_SECRET = crypto.randomBytes(32);
  refusalLru.clear();
}
