/**
 * Reproducible audit of MCP tool response sizes.
 *
 * Usage (from repo root, via mcp-server's npm script):
 *   cd mcp-server && npm run audit -- --vault <path>
 *
 * Output: JSON report on stdout. Convert to a markdown table in
 * docs/superpowers/specs/audit-2026-05-04-mcp-response-sizes.md.
 *
 * Tokens are counted via gpt-tokenizer (o200k_base, GPT-4o's BPE).
 * Anthropic's tokenizer isn't available as a local library; o200k_base
 * is a close proxy — typically within ±10% of Claude's count for
 * English/JSON text. The point of the audit is relative sizing across
 * tools, where this proxy is more than accurate enough.
 */

import { encode } from "gpt-tokenizer";

export interface ResponseMeasurement {
  bytes: number;
  approxTokens: number;
  entryCount: number;
  /**
   * Present iff the response was a cursor-protocol error envelope
   * (`{ error, message }`). Without this signal, the audit would silently
   * report the error envelope's tiny byte count as the tool's "response
   * size" — and a stale refusal LRU between runAudit calls would make
   * every capped tool look ~80 bytes wide.
   */
  error?: string;
}

export function measureResponse(response: unknown): ResponseMeasurement {
  const json = JSON.stringify(response);
  const bytes = Buffer.byteLength(json, "utf-8");
  const approxTokens = encode(json).length;
  let entryCount = 1;
  let error: string | undefined;

  // Error envelope detection (must precede the success-shape branches —
  // query_graph success returns `{columns, rows, rowCount, ...}`, no `error`).
  if (
    response !== null && typeof response === "object" && !Array.isArray(response) &&
    typeof (response as { error?: unknown }).error === "string"
  ) {
    error = (response as { error: string }).error;
    entryCount = 0;
  } else if (Array.isArray(response)) {
    entryCount = response.length;
  } else if (response !== null && typeof response === "object") {
    // Cursor-protocol responses wrap rows in a top-level array field:
    //   { entries: [...] }                     → search_memory
    //   { results: [...] }                     → search_notes
    //   { concepts: [...] }                    → list_concepts (PR 6)
    //   { domains: [...] }                     → list_domains (PR 6)
    //   { columns, rows, rowCount, cursor? }   → query_graph
    // Other tools return arrays directly (counted above) or single-shape
    // objects (entryCount stays 1).
    const obj = response as Record<string, unknown>;
    if (Array.isArray(obj.entries)) {
      entryCount = obj.entries.length;
    } else if (Array.isArray(obj.results)) {
      entryCount = obj.results.length;
    } else if (Array.isArray(obj.concepts)) {
      entryCount = obj.concepts.length;
    } else if (Array.isArray(obj.domains)) {
      entryCount = obj.domains.length;
    } else if (Array.isArray(obj.rows) && typeof obj.rowCount === "number") {
      // query_graph specifically — `rows` alone is too generic to assume;
      // require the `rowCount` field too so this branch only fires for
      // the query_graph shape and doesn't misfire on unrelated `{rows}` shapes.
      entryCount = obj.rowCount;
    }
  }
  return error === undefined
    ? { bytes, approxTokens, entryCount }
    : { bytes, approxTokens, entryCount, error };
}

import * as tools from "../mcp-server/dist/tools.js";
import { resetCursorForTesting } from "../mcp-server/dist/protocol/index.js";

export interface AuditReport {
  vault: string;
  generatedAt: string;
  tools: string[];
  measurements: Record<string, ResponseMeasurement>;
}

export async function runAudit(opts: {
  vault: string;
  searchQuery?: string;
}): Promise<AuditReport> {
  // Reset cursor refusal LRU + HMAC so repeated runAudit invocations in the
  // same process can't poison each other. Without this, a second invocation
  // within 300s would silently hit CURSOR_REQUIRED on every capped tool and
  // the audit would report error-envelope byte counts as "response size".
  resetCursorForTesting();

  const measurements: Record<string, ResponseMeasurement> = {};
  const searchQuery = opts.searchQuery ?? "fixture";

  // search_notes — typical "find what I worked on" query. The default
  // hits the test-fixture corpus; live audits should pass a query
  // representative of the target vault to exercise the FTS5 path.
  measurements.search_notes = measureResponse(
    await tools.search_notes(opts.vault, { query: searchQuery })
  );

  // list_concepts — default limit 50 in current code.
  measurements.list_concepts = measureResponse(
    await tools.list_concepts(opts.vault, {})
  );

  // list_domains — default limit 100, cap 500 (PR 6).
  measurements.list_domains = measureResponse(
    await tools.list_domains(opts.vault, {})
  );

  // query_graph — SELECT * FROM docs is the worst-case from #50.
  measurements.query_graph = measureResponse(
    await tools.query_graph(opts.vault, { sql: "SELECT * FROM docs" })
  );

  // get_context — measure all three depths so the spec sees deltas.
  for (const depth of ["minimal", "standard", "full"] as const) {
    measurements[`get_context_${depth}`] = measureResponse(
      await tools.get_context(opts.vault, { depth })
    );
  }

  // search_memory — default limit 50, returns full content (highest ROI target).
  measurements.search_memory = measureResponse(
    await tools.search_memory(opts.vault, { limit: 50 })
  );

  return {
    vault: opts.vault,
    generatedAt: new Date().toISOString(),
    tools: Object.keys(measurements),
    measurements,
  };
}

// CLI entry. Only fires when the script is invoked directly (e.g. via the
// mcp-server "audit" npm script); the test harness imports this module
// without ever matching this URL.
if (import.meta.url === `file://${process.argv[1]}`) {
  const vaultIdx = process.argv.indexOf("--vault");
  if (vaultIdx === -1) {
    console.error(
      "Usage: tsx scripts/audit_mcp_response_sizes.ts --vault <path> [--search-query <q>]"
    );
    process.exit(2);
  }
  const vault = process.argv[vaultIdx + 1];
  const sqIdx = process.argv.indexOf("--search-query");
  const searchQuery = sqIdx === -1 ? undefined : process.argv[sqIdx + 1];
  runAudit({ vault, searchQuery }).then(
    (r) => console.log(JSON.stringify(r, null, 2)),
    (err) => {
      console.error(err);
      process.exit(1);
    }
  );
}
