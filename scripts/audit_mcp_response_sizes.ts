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
}

export function measureResponse(response: unknown): ResponseMeasurement {
  const json = JSON.stringify(response);
  const bytes = Buffer.byteLength(json, "utf-8");
  const approxTokens = encode(json).length;
  const entryCount = Array.isArray(response) ? response.length : 1;
  return { bytes, approxTokens, entryCount };
}

import * as tools from "../mcp-server/dist/tools.js";

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

  // list_domains — currently unbounded.
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
