/**
 * vault.yaml ACL read-side — TypeScript port of cli/schist/acl.py.
 *
 * Only the read path is ported (parseVaultAcl, canWrite, scopeMatches,
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

export function deriveScope(filepath: string): string {
  // STUB — implemented in Task 4.
  void filepath;
  return "";
}

export function loadVaultAcl(vaultRoot: string): VaultAcl | null {
  // STUB — implemented in Task 3.
  void vaultRoot;
  return null;
}
