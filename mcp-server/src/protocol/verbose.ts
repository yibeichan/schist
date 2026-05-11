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
