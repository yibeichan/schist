/**
 * Reproducible audit of MCP tool response sizes.
 *
 * Usage (from repo root, via mcp-server's npm script):
 *   cd mcp-server && npm run audit -- --vault <path>
 *
 * Output: JSON report on stdout. Convert to a markdown table in
 * docs/superpowers/specs/audit-2026-05-04-mcp-response-sizes.md.
 *
 * The token approximation is intentionally crude (bytes/4). The point is
 * to compare relative sizes across tools, not to predict exact LLM cost.
 */

export interface ResponseMeasurement {
  bytes: number;
  approxTokens: number;
  entryCount: number;
}

export function measureResponse(response: unknown): ResponseMeasurement {
  const json = JSON.stringify(response);
  const bytes = Buffer.byteLength(json, "utf-8");
  const approxTokens = Math.round(bytes / 4);
  const entryCount = Array.isArray(response) ? response.length : 1;
  return { bytes, approxTokens, entryCount };
}

// CLI driver appended in Task 1.3.
