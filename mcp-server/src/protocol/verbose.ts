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
  // Whitespace-only or empty → not verbose, no error.
  // \s covers ASCII whitespace + NBSP (U+00A0); we also strip ZWS (U+200B)
  // and BOM (U+FEFF) which are invisible but not matched by \s in V8.
  const stripped = input.replace(/[​﻿]/gu, "");
  if (/^\s*$/u.test(stripped)) return { enabled: false };

  const trimmed = stripped.trim();
  // After strip+trim, re-check empty (defense in depth)
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
