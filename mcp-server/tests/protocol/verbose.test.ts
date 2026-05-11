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
