/**
 * Agent-identity policy — shared by tools.ts (MCP tool guard) and
 * sqlite-reader.ts (DB write guard). Both layers must agree, so the
 * policy lives in one place.
 *
 * Resolution order:
 *   1. SCHIST_ALLOWED_AGENTS defined → owner must appear in the
 *      comma-separated allowlist. Used by multi-agent shared-MCP
 *      deployments (e.g. OpenClaw) where one server process serves
 *      several agents and per-entry attribution is preserved by `owner`.
 *      Defined-but-empty ('') or all-whitespace values throw CONFIG_ERROR —
 *      to disable allowlist mode, unset the variable.
 *   2. SCHIST_ALLOWED_AGENTS unset, SCHIST_AGENT_ID set → owner must
 *      match exactly. Legacy single-agent path.
 *   3. Neither set → CONFIG_ERROR. Writes always require identity to
 *      be configured.
 */
/**
 * Resolves the active owner identity for read-side cursor handlers.
 *
 * Resolution order (matches the precedence callers picked individually
 * before #115 unified it):
 *   1. Per-call `owner` arg (when the handler accepts one — currently only
 *      `search_notes`, since vault.yaml participant scope-inherit lookup
 *      keys on the agent name)
 *   2. `SCHIST_AGENT_NAME` env var (human-readable, used by vault.yaml)
 *   3. `SCHIST_AGENT_ID` env var (stable id, used by memory + ACL paths)
 *   4. `""` empty string fallback
 *
 * Returning `""` is INTENTIONAL — anonymous reads are allowed (writes
 * still gate via `validateOwner`). The empty owner just means: this call
 * shares the refusal-LRU bucket with every other anonymous caller in the
 * same process. The startup warning in index.ts surfaces this state
 * when both env vars are missing.
 *
 * Before #115 the 5 cursor handlers each picked their own chain (3
 * different shapes across 5 handlers); see issue body for the divergence
 * matrix. Unified here so the next handler added inherits the correct
 * chain by default.
 */
export function resolveActiveOwner(perCallOwner?: string): string {
  return (
    perCallOwner ??
    process.env.SCHIST_AGENT_NAME ??
    process.env.SCHIST_AGENT_ID ??
    ""
  );
}

/**
 * Returns the canonical (whitespace-trimmed) owner string when validation
 * passes. Callers should assign the return value back so the canonical form
 * flows to downstream stamps (frontmatter `source_agent`, git commit subject,
 * `agent_memory.owner` column) — otherwise a caller passing `"atwood "`
 * would be accepted (the allowlist is trimmed at parse time) but store
 * `"atwood "` in those side tables, splitting per-agent state across two
 * keys that differ only by trailing whitespace. Pre-#131 the asymmetry
 * went the other way: allowlist trimmed, owner not, so `"atwood "` was
 * rejected. Both halves of that footgun are closed by canonicalizing here.
 */
export function validateOwner(owner: string): string {
  // Coerce non-string inputs ('' if undefined / null) so the comparisons
  // below behave predictably. The error messages still reflect the
  // raw value the caller sent for diagnostic clarity.
  const canonical = (typeof owner === "string" ? owner : "").trim();
  const allowedAgents = process.env.SCHIST_ALLOWED_AGENTS;
  if (allowedAgents !== undefined) {
    const allowed = allowedAgents
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    if (allowed.length === 0) {
      throw Object.assign(
        new Error(
          `SCHIST_ALLOWED_AGENTS is defined but parses to an empty list (value: '${allowedAgents}'). To disable allowlist mode, unset the variable.`
        ),
        { error: "CONFIG_ERROR" }
      );
    }
    if (!allowed.includes(canonical)) {
      throw Object.assign(
        new Error(
          `Owner '${owner}' not in SCHIST_ALLOWED_AGENTS '${allowedAgents}'`
        ),
        { error: "VALIDATION_ERROR" }
      );
    }
    return canonical;
  }
  const agentId = process.env.SCHIST_AGENT_ID;
  if (!agentId) {
    throw Object.assign(
      new Error(
        "SCHIST_AGENT_ID or SCHIST_ALLOWED_AGENTS env var is required for write operations"
      ),
      { error: "CONFIG_ERROR" }
    );
  }
  if (agentId !== canonical) {
    throw Object.assign(
      new Error(`Owner '${owner}' does not match SCHIST_AGENT_ID '${agentId}'`),
      { error: "VALIDATION_ERROR" }
    );
  }
  return canonical;
}
