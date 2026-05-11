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

  it("returns enabled:false for NBSP-only string (U+00A0)", () => {
    expect(parseVerbose(" ".repeat(12))).toEqual({ enabled: false });
  });

  it("returns enabled:false for ZWS-only string (U+200B)", () => {
    expect(parseVerbose("​".repeat(12))).toEqual({ enabled: false });
  });

  it("returns enabled:false for BOM-only string (U+FEFF)", () => {
    expect(parseVerbose("﻿".repeat(12))).toEqual({ enabled: false });
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
    const reason = "学习深度学习中的注意力机"; // 12 chars
    expect([...reason].length).toBe(12); // sanity
    expect(parseVerbose(reason)).toEqual({ enabled: true, reason });
  });
});

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
    const calls = (stderrSpy.mock.calls as unknown[][]).map((c) => String(c[0]));
    // owner is JSON.stringify'd so it appears as "yibei" (with quotes) in the output
    const matched = calls.find((s) => s.includes("[verbose] search_memory by") && s.includes("yibei"));
    expect(matched).toBeDefined();
    expect(matched).toContain('"investigating bug"'); // JSON.stringify quoted
  });

  it("uses '<anonymous>' when owner is empty string", () => {
    logVerbose({ tool: "search_memory", owner: "", reason: "investigating bug" });
    const calls = (stderrSpy.mock.calls as unknown[][]).map((c) => String(c[0]));
    expect(calls.find((s) => s.includes("[verbose] search_memory by <anonymous>:"))).toBeDefined();
  });

  it("escapes newlines in reason via JSON.stringify (defends against log injection)", () => {
    logVerbose({ tool: "search_memory", owner: "yibei", reason: "benign\n[error] root pwned" });
    const calls = (stderrSpy.mock.calls as unknown[][]).map((c) => String(c[0]));
    const matched = calls.find((s) => s.includes("[verbose]"));
    expect(matched).toBeDefined();
    // JSON.stringify escapes newline to "\n" (literal backslash-n, no real newline)
    expect(matched).toContain('"benign\\n[error] root pwned"');
    // No INJECTED line — splitting on real newlines yields at most the one [verbose] line + empty trailing
    // The [error] text appears only inside the JSON-escaped string, not as a standalone injected line
    const lines = matched!.split("\n").filter((l: string) => l.trim() !== "" && !l.includes("[verbose]"));
    expect(lines.filter((l: string) => l.includes("[error]"))).toHaveLength(0); // no injected line
  });

  it("escapes control characters in reason", () => {
    logVerbose({ tool: "x", owner: "y", reason: "with \x00null\x1bescape" });
    const calls = (stderrSpy.mock.calls as unknown[][]).map((c) => String(c[0]));
    const matched = calls.find((s) => s.includes("[verbose]"));
    expect(matched).toContain("\\u0000");
    expect(matched).toContain("\\u001b"); // JSON.stringify uses lowercase hex for control chars
  });

  it("escapes the owner field as well (caller-controlled)", () => {
    logVerbose({ tool: "x", owner: "weird\nowner", reason: "investigating bug" });
    const calls = (stderrSpy.mock.calls as unknown[][]).map((c) => String(c[0]));
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
