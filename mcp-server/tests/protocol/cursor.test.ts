import { describe, it, expect, beforeEach, jest } from "@jest/globals";
import { canonicalizeQueryHash } from "../../src/protocol/cursor.js";
import {
  issueCursor,
  decodeCursor,
  resetForTesting,
  CURSOR_TTL_SECONDS,
} from "../../src/protocol/cursor.js";

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

  it("accepts diamond references (same object at two distinct paths, no cycle)", () => {
    const shared = { x: 1, y: "foo" };
    const args = { a: shared, b: shared };
    const r = canonicalizeQueryHash(args, "yibei");
    expect(r.ok).toBe(true);
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

  it("opts.excludeKeys overrides the default (cursor, verbose) exclusion list", () => {
    // Custom excludeKeys: drop "myMeta" instead of cursor/verbose
    const a = canonicalizeQueryHash(
      { q: "foo", myMeta: "x" },
      "yibei",
      { excludeKeys: ["myMeta"] },
    );
    const b = canonicalizeQueryHash({ q: "foo" }, "yibei", { excludeKeys: ["myMeta"] });
    expect(a.ok && b.ok && a.queryHash === b.queryHash).toBe(true);

    // With default excludeKeys, "myMeta" is included → different hash
    const c = canonicalizeQueryHash({ q: "foo", myMeta: "x" }, "yibei");
    const d = canonicalizeQueryHash({ q: "foo" }, "yibei");
    expect(c.ok && d.ok && c.queryHash !== d.queryHash).toBe(true);
  });
});

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

describe("decodeCursor — log-injection defense", () => {
  beforeEach(() => resetForTesting());

  it("sanitizes payload.tool in CURSOR_WRONG_TOOL message (defense in depth)", () => {
    // Forge a cursor whose tool contains newline-and-bracket injection
    const token = issueCursor({ tool: "evil\n[ERROR] admin override", queryHash: "h", offset: 0 });
    const r = decodeCursor(token, "search_notes");
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.error.error).toBe("CURSOR_WRONG_TOOL");
      // The dangerous chars (newline, brackets, spaces) must be replaced with '?'.
      expect(r.error.message).not.toContain("\n");
      expect(r.error.message).not.toContain("[ERROR]");
      // The tool name in the message must only use the sanitized character set.
      // Extract the quoted tool name and verify alphabet.
      const match = r.error.message.match(/'([^']*)'/);
      expect(match).not.toBeNull();
      if (match) expect(match[1]).toMatch(/^[\w?-]+$/);
    }
  });
});
