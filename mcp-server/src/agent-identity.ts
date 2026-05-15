/**
 * Agent-identity policy — shared by tools.ts (MCP tool guard) and
 * sqlite-reader.ts (DB write guard). Both layers must agree, so the
 * policy lives in one place.
 *
 * Resolution order:
 *   1. SCHIST_ALLOWED_AGENTS set → owner must appear in the
 *      comma-separated allowlist. Used by multi-agent shared-MCP
 *      deployments (e.g. OpenClaw) where one server process serves
 *      several agents and per-entry attribution is preserved by `owner`.
 *   2. SCHIST_ALLOWED_AGENTS unset, SCHIST_AGENT_ID set → owner must
 *      match exactly. Legacy single-agent path.
 *   3. Neither set → CONFIG_ERROR. Writes always require identity to
 *      be configured.
 */
export function validateOwner(owner: string): void {
  const allowedAgents = process.env.SCHIST_ALLOWED_AGENTS;
  if (allowedAgents) {
    const allowed = allowedAgents
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    if (allowed.length === 0) {
      throw Object.assign(
        new Error(
          `SCHIST_ALLOWED_AGENTS is set but parses to an empty list (value: '${allowedAgents}')`
        ),
        { error: "CONFIG_ERROR" }
      );
    }
    if (!allowed.includes(owner)) {
      throw Object.assign(
        new Error(
          `Owner '${owner}' not in SCHIST_ALLOWED_AGENTS '${allowedAgents}'`
        ),
        { error: "VALIDATION_ERROR" }
      );
    }
    return;
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
  if (agentId !== owner) {
    throw Object.assign(
      new Error(`Owner '${owner}' does not match SCHIST_AGENT_ID '${agentId}'`),
      { error: "VALIDATION_ERROR" }
    );
  }
}
