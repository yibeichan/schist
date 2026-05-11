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
