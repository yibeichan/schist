/**
 * vault.yaml ACL read-side — TypeScript port of cli/schist/acl.py.
 *
 * Only the read path is ported (loadVaultAcl, canWrite, scopeMatches,
 * deriveScope). The hub's pre-receive remains the trust boundary that
 * validates vault.yaml content; this module is a UX optimisation that
 * fails fast locally before a doomed commit lands.
 *
 * Parity with the Python implementation is pinned by the shared fixtures
 * in cli/schist/acl-fixtures/, loaded by both
 * mcp-server/tests/vault-acl.test.ts and cli/tests/test_acl_parity.py.
 *
 * See docs/superpowers/specs/2026-05-28-mcp-hub-acl-intersection-design.md.
 */

import { readFileSync } from "fs";
import * as path from "path";
import { load as yamlLoad } from "js-yaml";

export interface AccessEntry {
  read: string[];
  write: string[];
}

export interface VaultAcl {
  access: Record<string, AccessEntry>;
}

export function scopeMatches(allowed: string[], target: string): boolean {
  for (const scope of allowed) {
    if (scope === "*") return true;
    if (scope === target) return true;
    if (target.startsWith(scope + "/")) return true;
  }
  return false;
}

export function canWrite(acl: VaultAcl, identity: string, scope: string): boolean {
  const entry = acl.access[identity];
  if (!entry) return false;
  return scopeMatches(entry.write, scope);
}

/**
 * Resolve the identity used for the vault.yaml ACL lookup — the SAME identity
 * the hub's pre-receive resolves (cli/schist/pre_receive.py:resolve_identity):
 * SCHIST_IDENTITY, then GL_USER. This is the per-MACHINE/spoke identity that
 * `schist sync push` forwards to the hub — NOT the per-AGENT SCHIST_AGENT_ID.
 *
 * vault.yaml's `access` map is keyed by machine identity (e.g. `dragonfly`),
 * so the local intersection (#155) must look up the machine identity too;
 * keying it on the agent owner (e.g. `claude-desktop`) produces a FALSE
 * ACL_DENIED on writes the hub would actually accept.
 *
 * Falls back to `fallback` (the calling agent's owner id) only when no machine
 * identity is configured — preserving single-axis deployments where the
 * vault.yaml participant is named after the agent.
 *
 * Known gap (pre-existing): on a SPOKE with neither env var set, this falls
 * back to owner and may locally ALLOW a write the hub then rejects ("cannot
 * determine push identity", pre_receive.py:286). That surfaces as a delayed
 * push failure / syncWarning rather than an upfront ACL_DENIED — git stays
 * canonical, so it's a sync hiccup, not corruption. A misconfigured spoke is
 * the real fault; `schist doctor` is the place to fail-fast on it.
 */
export function resolveAclIdentity(fallback: string): string {
  return process.env.SCHIST_IDENTITY || process.env.GL_USER || fallback;
}

/**
 * Derive the ACL scope from a file path. Mirrors
 * cli/schist/pre_receive.py:derive_scope verbatim.
 *
 *   notes/2026-05-28-foo.md   → "notes"
 *   projects/foo/bar.md        → "projects/foo"
 *   vault.yaml                 → ""        (root-level)
 *
 * The returned scope is the directory portion of the path, POSIX-
 * normalised so callers that pass "./foo" get the same answer as "foo".
 * Path-traversal segments are NOT defended against here; the upstream
 * assertPathSafe guard in git-writer.ts rejects "../" before this is
 * reached.
 */
export function deriveScope(filepath: string): string {
  const normalized = path.posix.normalize(filepath);
  const parent = path.posix.dirname(normalized);
  return parent === "." ? "" : parent;
}

function isENOENT(e: unknown): boolean {
  return e !== null && typeof e === "object" && "code" in e &&
    (e as { code: string }).code === "ENOENT";
}

/**
 * Read <vaultRoot>/vault.yaml and return a minimal VaultAcl.
 *
 * Returns null in three "fall-open" cases (the caller skips the ACL
 * check entirely):
 *   1. vault.yaml does not exist (backward compat — standalone vaults,
 *      pre-vault.yaml deployments, existing MCP test fixtures).
 *      If vault.yaml is ever made mandatory (e.g. a future schist v0.3
 *      invariant), remove this branch and let the caller throw.
 *   2. vault.yaml is unreadable / unparseable as YAML — warn + skip.
 *   3. vault.yaml is valid YAML but missing the 'access' mapping —
 *      warn + skip.
 *
 * Asymmetric with cli/schist/rate_limit.py (fail-closed) by design:
 * the MCP-side check is a UX optimisation; the hub's pre-receive is
 * the trust boundary. Logging-and-skipping matches the existing
 * fail-open posture in tools.ts:loadCanonicalDirectories.
 */
export function loadVaultAcl(vaultRoot: string): VaultAcl | null {
  const aclPath = path.join(vaultRoot, "vault.yaml");
  let raw: unknown;
  try {
    raw = yamlLoad(readFileSync(aclPath, "utf-8"));
  } catch (e) {
    if (isENOENT(e)) return null;
    const msg = e instanceof Error ? e.message : String(e);
    console.warn(`schist: vault.yaml unreadable (${msg}); skipping local ACL check.`);
    return null;
  }

  if (
    !raw || typeof raw !== "object" ||
    !("access" in (raw as object)) ||
    typeof (raw as { access: unknown }).access !== "object" ||
    (raw as { access: unknown }).access === null
  ) {
    console.warn(`schist: vault.yaml at ${aclPath} is missing the 'access' mapping; skipping local ACL check.`);
    return null;
  }

  const access: VaultAcl["access"] = {};
  for (const [identity, entry] of Object.entries((raw as { access: Record<string, unknown> }).access)) {
    if (entry && typeof entry === "object") {
      const e = entry as { read?: unknown; write?: unknown };
      access[identity] = {
        read: Array.isArray(e.read) ? e.read.map((v) => (v == null ? "" : String(v))) : [],
        write: Array.isArray(e.write) ? e.write.map((v) => (v == null ? "" : String(v))) : [],
      };
    }
  }
  return { access };
}
