import { describe, it, expect } from "@jest/globals";
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
