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

// Imports below are used by loadVaultAcl (Task 3) and deriveScope (Task 4),
// which currently exist as STUBs. Keep them so those tasks plug in cleanly.
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

export function deriveScope(filepath: string): string {
  // STUB — implemented in Task 4.
  void filepath;
  return "";
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
