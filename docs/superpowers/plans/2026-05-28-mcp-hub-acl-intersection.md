# MCP↔hub-ACL intersection (#155) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the MCP server consult `vault.yaml`'s per-participant write grants before `create_note`/`add_connection`, so a spoke can never produce a local commit the hub will reject. Add a spoke-side `schist doctor` check that surfaces schema↔ACL drift before the user hits the failure path.

**Architecture:** Port the read-side of `cli/schist/acl.py` to a small TypeScript module (`mcp-server/src/vault-acl.ts`, ~80 LoC). Both implementations are pinned to a shared YAML+JSON fixture directory (`cli/schist/acl-fixtures/`) so the contract is data, not code. Missing/malformed `vault.yaml` → log + skip the check (fail-open; hub remains the trust boundary). Wire one guard into both vault-write tools; reject with new `ACL_DENIED` error.

**Tech Stack:** TypeScript (Node 22 / ESM, Jest, ts-jest, `js-yaml` already a dep), Python 3.12 (pytest, PyYAML).

**Spec:** `docs/superpowers/specs/2026-05-28-mcp-hub-acl-intersection-design.md`

**Branch:** `feat/155-mcp-hub-acl-intersection` (already created from main; spec already committed at `e09cf12`).

---

## Task 1: Shared parity fixtures (Python-loaded first)

**Files:**
- Create: `cli/schist/acl-fixtures/basic-write-grant.yaml`
- Create: `cli/schist/acl-fixtures/basic-write-grant.cases.json`
- Create: `cli/schist/acl-fixtures/wildcard-write.yaml`
- Create: `cli/schist/acl-fixtures/wildcard-write.cases.json`
- Create: `cli/schist/acl-fixtures/parent-grants-child.yaml`
- Create: `cli/schist/acl-fixtures/parent-grants-child.cases.json`
- Create: `cli/schist/acl-fixtures/unknown-identity.yaml`
- Create: `cli/schist/acl-fixtures/unknown-identity.cases.json`
- Create: `cli/schist/acl-fixtures/README.md`
- Test: `cli/tests/test_acl_parity.py` (new)
- Modify: `cli/pyproject.toml` (package-data — ship the fixtures in the wheel)

- [ ] **Step 1: Create the fixtures directory and a README**

`cli/schist/acl-fixtures/README.md`:

```markdown
# ACL parity fixtures

Each case is a pair of files: `<name>.yaml` is a `vault.yaml` snippet, and
`<name>.cases.json` is a list of `{ identity, scope, canWrite }` tuples.

Both `cli/tests/test_acl_parity.py` (Python) and
`mcp-server/tests/vault-acl.test.ts` (TypeScript) load every pair and assert
their respective `can_write` / `canWrite` implementations return the
expected boolean for every case.

To add a new ACL rule (e.g. a new wildcard syntax or a new scope shape),
add a new fixture pair here — both implementations will pick it up
automatically.

These are SHIPPED in the schist wheel as package data so the fixtures are
available to consumers of the installed CLI. The TS side reads them via a
relative path from the schist source tree.
```

- [ ] **Step 2: Write the first fixture pair (basic-write-grant)**

`cli/schist/acl-fixtures/basic-write-grant.yaml`:

```yaml
vault_version: 1
name: test-basic
scope_convention: flat
participants:
  - name: alice
    type: spoke
    default_scope: global
  - name: bob
    type: spoke
    default_scope: global
access:
  alice:
    read: ["*"]
    write: [notes, papers]
  bob:
    read: [notes]
    write: [notes]
```

`cli/schist/acl-fixtures/basic-write-grant.cases.json`:

```json
[
  { "identity": "alice", "scope": "notes", "canWrite": true },
  { "identity": "alice", "scope": "papers", "canWrite": true },
  { "identity": "alice", "scope": "logs", "canWrite": false },
  { "identity": "bob", "scope": "notes", "canWrite": true },
  { "identity": "bob", "scope": "papers", "canWrite": false },
  { "identity": "carol", "scope": "notes", "canWrite": false }
]
```

- [ ] **Step 3: Write the wildcard-write fixture pair**

`cli/schist/acl-fixtures/wildcard-write.yaml`:

```yaml
vault_version: 1
name: test-wildcard
scope_convention: flat
participants:
  - name: admin
    type: agent
    default_scope: global
access:
  admin:
    read: ["*"]
    write: ["*"]
```

`cli/schist/acl-fixtures/wildcard-write.cases.json`:

```json
[
  { "identity": "admin", "scope": "notes", "canWrite": true },
  { "identity": "admin", "scope": "logs", "canWrite": true },
  { "identity": "admin", "scope": "projects/foo", "canWrite": true },
  { "identity": "admin", "scope": "", "canWrite": true },
  { "identity": "other", "scope": "notes", "canWrite": false }
]
```

- [ ] **Step 4: Write the parent-grants-child fixture pair**

`cli/schist/acl-fixtures/parent-grants-child.yaml`:

```yaml
vault_version: 1
name: test-parent
scope_convention: flat
participants:
  - name: orcd
    type: spoke
    default_scope: global
access:
  orcd:
    read: ["*"]
    write: [projects, research]
```

`cli/schist/acl-fixtures/parent-grants-child.cases.json`:

```json
[
  { "identity": "orcd", "scope": "projects", "canWrite": true },
  { "identity": "orcd", "scope": "projects/brain-states-friends", "canWrite": true },
  { "identity": "orcd", "scope": "projects/brain-states-friends/subnotes", "canWrite": true },
  { "identity": "orcd", "scope": "research", "canWrite": true },
  { "identity": "orcd", "scope": "researchx", "canWrite": false },
  { "identity": "orcd", "scope": "notes", "canWrite": false }
]
```

Note: `researchx` is intentionally a non-grant — `_scope_matches` uses `startswith(scope + "/")`, not `startswith(scope)`, so `research` should not grant `researchx`. This case pins that behavior.

- [ ] **Step 5: Write the unknown-identity fixture pair**

`cli/schist/acl-fixtures/unknown-identity.yaml`:

```yaml
vault_version: 1
name: test-unknown
scope_convention: flat
participants:
  - name: known
    type: spoke
    default_scope: global
access:
  known:
    read: [notes]
    write: [notes]
```

`cli/schist/acl-fixtures/unknown-identity.cases.json`:

```json
[
  { "identity": "known", "scope": "notes", "canWrite": true },
  { "identity": "unknown-agent", "scope": "notes", "canWrite": false },
  { "identity": "", "scope": "notes", "canWrite": false }
]
```

- [ ] **Step 6: Write a failing Python parity test**

`cli/tests/test_acl_parity.py` (new):

```python
"""Parity fixtures: assert VaultACL.can_write matches the cases.json contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from schist.acl import parse_vault_yaml

FIXTURES_DIR = Path(__file__).parent.parent / "schist" / "acl-fixtures"


def _fixture_pairs() -> list[tuple[Path, Path]]:
    return sorted(
        (yaml_path, yaml_path.with_suffix(".cases.json"))
        for yaml_path in FIXTURES_DIR.glob("*.yaml")
    )


@pytest.mark.parametrize("yaml_path,cases_path", _fixture_pairs())
def test_can_write_matches_fixture(yaml_path: Path, cases_path: Path) -> None:
    acl = parse_vault_yaml(yaml_path)
    cases = json.loads(cases_path.read_text())
    for case in cases:
        actual = acl.can_write(case["identity"], case["scope"])
        assert actual == case["canWrite"], (
            f"{yaml_path.name}: identity={case['identity']!r} "
            f"scope={case['scope']!r} expected {case['canWrite']} got {actual}"
        )
```

- [ ] **Step 7: Run the parity test — expect it to PASS**

Run:

```bash
cd /orcd/home/002/yibei/schist
uv run --with pytest --with ./cli python -m pytest cli/tests/test_acl_parity.py -v
```

Expected: 4 parametrized cases, all PASS. (The Python `can_write` is already correct; the fixtures encode its existing behavior.)

If any case fails, the fixture is wrong — fix the fixture or the case to match `_scope_matches`'s documented behavior in `cli/schist/acl.py:93-109`.

- [ ] **Step 8: Update pyproject.toml to ship the fixtures**

Modify `cli/pyproject.toml` — find the `[tool.setuptools.package-data]` block and extend the schist entry:

```toml
[tool.setuptools.package-data]
schist = ["*.sql", "default.yaml", "acl-fixtures/*.yaml", "acl-fixtures/*.json", "acl-fixtures/*.md"]
```

Verify the existing line for the exact prefix; do not duplicate keys.

- [ ] **Step 9: Verify the wheel ships the fixtures**

Run:

```bash
cd /orcd/home/002/yibei/schist/cli
uv build --wheel -o /tmp/schist-wheel 2>&1 | tail -5
python3 -c "
import zipfile
with zipfile.ZipFile(sorted(__import__('pathlib').Path('/tmp/schist-wheel').glob('*.whl'))[-1]) as z:
    for n in sorted(z.namelist()):
        if 'acl-fixtures' in n:
            print(n)
"
rm -rf /tmp/schist-wheel
```

Expected: a list of `schist/acl-fixtures/*.yaml` + `.cases.json` + `README.md` entries inside the wheel. If empty, the package-data glob didn't match — recheck the toml.

- [ ] **Step 10: Commit**

```bash
cd /orcd/home/002/yibei/schist
git add cli/schist/acl-fixtures cli/tests/test_acl_parity.py cli/pyproject.toml
git commit -m "test(cli): shared ACL parity fixtures + Python loader (#155)"
```

---

## Task 2: TS port — types, scopeMatches, canWrite

**Files:**
- Create: `mcp-server/src/vault-acl.ts`
- Test: `mcp-server/tests/vault-acl.test.ts`

- [ ] **Step 1: Stub the TS module with types only**

`mcp-server/src/vault-acl.ts` (new):

```typescript
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
  // STUB — implemented in Task 2 Step 3.
  void allowed;
  void target;
  return false;
}

export function canWrite(acl: VaultAcl, identity: string, scope: string): boolean {
  // STUB — implemented in Task 2 Step 5.
  void acl;
  void identity;
  void scope;
  return false;
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
```

- [ ] **Step 2: Write failing tests for scopeMatches**

`mcp-server/tests/vault-acl.test.ts` (new):

```typescript
import { describe, expect, test } from "@jest/globals";
import { scopeMatches } from "../src/vault-acl.js";

describe("scopeMatches", () => {
  test("exact match returns true", () => {
    expect(scopeMatches(["notes"], "notes")).toBe(true);
  });
  test("wildcard matches anything", () => {
    expect(scopeMatches(["*"], "anything")).toBe(true);
    expect(scopeMatches(["*"], "")).toBe(true);
  });
  test("parent grants child via slash", () => {
    expect(scopeMatches(["projects"], "projects/foo")).toBe(true);
    expect(scopeMatches(["projects"], "projects/foo/bar")).toBe(true);
  });
  test("prefix without slash boundary does NOT match", () => {
    // 'research' does not grant 'researchx'
    expect(scopeMatches(["research"], "researchx")).toBe(false);
  });
  test("empty allowed array returns false", () => {
    expect(scopeMatches([], "notes")).toBe(false);
  });
  test("no match in non-empty list returns false", () => {
    expect(scopeMatches(["notes", "papers"], "logs")).toBe(false);
  });
});
```

- [ ] **Step 3: Run the tests — expect FAIL**

Run:

```bash
cd /orcd/home/002/yibei/schist/mcp-server
npm run build && npm test -- --testPathPatterns=vault-acl.test 2>&1 | tail -15
```

Expected: build succeeds; tests fail (`scopeMatches` returns false for everything).

- [ ] **Step 4: Implement scopeMatches**

Replace the `scopeMatches` STUB in `mcp-server/src/vault-acl.ts`:

```typescript
export function scopeMatches(allowed: string[], target: string): boolean {
  for (const scope of allowed) {
    if (scope === "*") return true;
    if (scope === target) return true;
    if (target.startsWith(scope + "/")) return true;
  }
  return false;
}
```

- [ ] **Step 5: Run scopeMatches tests — expect PASS**

```bash
npm run build && npm test -- --testPathPatterns=vault-acl.test 2>&1 | tail -8
```

Expected: 6/6 pass for the scopeMatches describe block.

- [ ] **Step 6: Add failing tests for canWrite**

Append to `mcp-server/tests/vault-acl.test.ts`:

```typescript
import { canWrite, type VaultAcl } from "../src/vault-acl.js";

describe("canWrite", () => {
  const acl: VaultAcl = {
    access: {
      alice: { read: ["*"], write: ["notes", "papers"] },
      admin: { read: ["*"], write: ["*"] },
    },
  };

  test("granted scope returns true", () => {
    expect(canWrite(acl, "alice", "notes")).toBe(true);
  });
  test("ungranted scope returns false", () => {
    expect(canWrite(acl, "alice", "logs")).toBe(false);
  });
  test("unknown identity returns false", () => {
    expect(canWrite(acl, "carol", "notes")).toBe(false);
  });
  test("wildcard write grants every scope", () => {
    expect(canWrite(acl, "admin", "anything")).toBe(true);
    expect(canWrite(acl, "admin", "")).toBe(true);
  });
});
```

(Add the `canWrite, type VaultAcl` import to the existing line: `import { scopeMatches, canWrite, type VaultAcl } from "../src/vault-acl.js";`.)

- [ ] **Step 7: Run — expect canWrite tests FAIL**

```bash
npm run build && npm test -- --testPathPatterns=vault-acl.test 2>&1 | tail -10
```

Expected: scopeMatches still passes; canWrite cases fail.

- [ ] **Step 8: Implement canWrite**

Replace the STUB:

```typescript
export function canWrite(acl: VaultAcl, identity: string, scope: string): boolean {
  const entry = acl.access[identity];
  if (!entry) return false;
  return scopeMatches(entry.write, scope);
}
```

- [ ] **Step 9: Run — expect all pass**

```bash
npm run build && npm test -- --testPathPatterns=vault-acl.test 2>&1 | tail -8
```

Expected: all 10 tests pass (6 scopeMatches + 4 canWrite).

- [ ] **Step 10: Commit**

```bash
cd /orcd/home/002/yibei/schist
git add mcp-server/src/vault-acl.ts mcp-server/tests/vault-acl.test.ts
git commit -m "feat(mcp-server): vault-acl types + scopeMatches + canWrite (#155)"
```

---

## Task 3: TS port — loadVaultAcl (parser with fail-open)

**Files:**
- Modify: `mcp-server/src/vault-acl.ts`
- Modify: `mcp-server/tests/vault-acl.test.ts`

- [ ] **Step 1: Write failing tests for loadVaultAcl**

Append to `mcp-server/tests/vault-acl.test.ts`:

```typescript
import * as fs from "fs/promises";
import * as os from "os";
import { loadVaultAcl } from "../src/vault-acl.js";
import { promises as fsp } from "fs";

// Update earlier import: scopeMatches, canWrite, type VaultAcl, loadVaultAcl

const tmpDirs: string[] = [];
afterAll(async () => {
  for (const d of tmpDirs) {
    await fs.rm(d, { recursive: true, force: true });
  }
});

async function makeTempVault(vaultYaml: string | null): Promise<string> {
  const dir = await fs.mkdtemp(`${os.tmpdir()}/schist-acl-`);
  tmpDirs.push(dir);
  if (vaultYaml !== null) {
    await fs.writeFile(`${dir}/vault.yaml`, vaultYaml, "utf-8");
  }
  return dir;
}

describe("loadVaultAcl", () => {
  test("returns null when vault.yaml is missing", async () => {
    const dir = await makeTempVault(null);
    expect(loadVaultAcl(dir)).toBeNull();
  });

  test("returns null and logs warning on malformed YAML", async () => {
    const dir = await makeTempVault(":::not yaml:::");
    const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
    expect(loadVaultAcl(dir)).toBeNull();
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  test("returns null when 'access' is missing", async () => {
    const dir = await makeTempVault("name: nope\n");
    const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
    expect(loadVaultAcl(dir)).toBeNull();
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  test("parses a valid vault.yaml", async () => {
    const dir = await makeTempVault(`
vault_version: 1
name: t
scope_convention: flat
participants: [{name: alice}]
access:
  alice:
    read: ["*"]
    write: [notes, papers]
`);
    const acl = loadVaultAcl(dir);
    expect(acl).not.toBeNull();
    expect(acl!.access.alice.write).toEqual(["notes", "papers"]);
    expect(acl!.access.alice.read).toEqual(["*"]);
  });

  test("coerces non-string write entries defensively", async () => {
    // If vault.yaml has weird types, fall back to empty list rather than crash.
    const dir = await makeTempVault(`
access:
  alice:
    read: ["*"]
    write: [notes, 42, null]
`);
    const acl = loadVaultAcl(dir);
    expect(acl).not.toBeNull();
    expect(acl!.access.alice.write).toEqual(["notes", "42", ""]);
  });
});
```

Also add `jest` to the existing `@jest/globals` import line: `import { describe, expect, test, afterAll, jest } from "@jest/globals";`.

- [ ] **Step 2: Run — expect loadVaultAcl tests FAIL**

```bash
cd /orcd/home/002/yibei/schist/mcp-server
npm run build && npm test -- --testPathPatterns=vault-acl.test 2>&1 | tail -15
```

Expected: existing 10 still pass; 5 loadVaultAcl tests fail.

- [ ] **Step 3: Implement loadVaultAcl**

Replace the `loadVaultAcl` STUB in `mcp-server/src/vault-acl.ts`:

```typescript
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
```

- [ ] **Step 4: Run — expect all pass**

```bash
npm run build && npm test -- --testPathPatterns=vault-acl.test 2>&1 | tail -8
```

Expected: 15 tests pass (6 scopeMatches + 4 canWrite + 5 loadVaultAcl).

- [ ] **Step 5: Commit**

```bash
cd /orcd/home/002/yibei/schist
git add mcp-server/src/vault-acl.ts mcp-server/tests/vault-acl.test.ts
git commit -m "feat(mcp-server): vault-acl loadVaultAcl with fail-open posture (#155)"
```

---

## Task 4: TS port — deriveScope

**Files:**
- Modify: `mcp-server/src/vault-acl.ts`
- Modify: `mcp-server/tests/vault-acl.test.ts`

- [ ] **Step 1: Write failing tests for deriveScope**

Append to `mcp-server/tests/vault-acl.test.ts`:

```typescript
import { deriveScope } from "../src/vault-acl.js";
// (or extend the existing import line accordingly)

describe("deriveScope", () => {
  test("top-level file under a directory returns the directory", () => {
    expect(deriveScope("notes/2026-05-28-foo.md")).toBe("notes");
  });
  test("nested directory returns the full parent path", () => {
    expect(deriveScope("projects/foo/2026-05-28-bar.md")).toBe("projects/foo");
  });
  test("deeply nested path", () => {
    expect(deriveScope("projects/foo/sub/2026-05-28-baz.md")).toBe("projects/foo/sub");
  });
  test("root-level file returns empty string", () => {
    expect(deriveScope("vault.yaml")).toBe("");
  });
  test("leading ./ is normalised away", () => {
    expect(deriveScope("./notes/foo.md")).toBe("notes");
  });
  test("trailing slash in input is normalised", () => {
    expect(deriveScope("notes/")).toBe("");
  });
});
```

- [ ] **Step 2: Run — expect FAIL**

```bash
cd /orcd/home/002/yibei/schist/mcp-server
npm run build && npm test -- --testPathPatterns=vault-acl.test 2>&1 | tail -10
```

Expected: deriveScope tests fail (stub returns empty for all).

- [ ] **Step 3: Implement deriveScope**

Replace the STUB:

```typescript
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
```

- [ ] **Step 4: Run — expect all pass**

```bash
npm run build && npm test -- --testPathPatterns=vault-acl.test 2>&1 | tail -8
```

Expected: 21 tests pass total (6 + 4 + 5 + 6).

- [ ] **Step 5: Commit**

```bash
cd /orcd/home/002/yibei/schist
git add mcp-server/src/vault-acl.ts mcp-server/tests/vault-acl.test.ts
git commit -m "feat(mcp-server): vault-acl deriveScope (#155)"
```

---

## Task 5: TS-side parity test against shared fixtures

**Files:**
- Modify: `mcp-server/tests/vault-acl.test.ts`

- [ ] **Step 1: Add the parity test block**

Append to `mcp-server/tests/vault-acl.test.ts`:

```typescript
import { readdirSync, readFileSync as readFileSyncForFixtures } from "fs";
import { join as pathJoin, dirname as pathDirname } from "path";
import { fileURLToPath } from "url";

// Fixtures live at <repo>/cli/schist/acl-fixtures/. The test file is at
// <repo>/mcp-server/tests/vault-acl.test.ts → walk up two directories.
const FIXTURES_DIR_TS = pathJoin(
  pathDirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "cli",
  "schist",
  "acl-fixtures",
);

interface ParityCase {
  identity: string;
  scope: string;
  canWrite: boolean;
}

describe("vault-acl parity fixtures", () => {
  const yamlFiles = readdirSync(FIXTURES_DIR_TS).filter((f) => f.endsWith(".yaml"));
  // Sanity-check: discover at least the four fixtures from Task 1.
  test("discovers parity fixtures", () => {
    expect(yamlFiles.length).toBeGreaterThanOrEqual(4);
  });

  for (const yamlFile of yamlFiles) {
    const base = yamlFile.replace(/\.yaml$/, "");
    test(`${base}: TS canWrite matches every case in ${base}.cases.json`, async () => {
      // Build a temp vault containing JUST this fixture as vault.yaml.
      const dir = await fs.mkdtemp(`${os.tmpdir()}/schist-parity-${base}-`);
      tmpDirs.push(dir);
      const yamlBody = readFileSyncForFixtures(pathJoin(FIXTURES_DIR_TS, yamlFile), "utf-8");
      await fs.writeFile(pathJoin(dir, "vault.yaml"), yamlBody, "utf-8");

      const acl = loadVaultAcl(dir);
      expect(acl).not.toBeNull();

      const cases: ParityCase[] = JSON.parse(
        readFileSyncForFixtures(pathJoin(FIXTURES_DIR_TS, `${base}.cases.json`), "utf-8"),
      );
      for (const c of cases) {
        expect({ ...c, actual: canWrite(acl!, c.identity, c.scope) }).toEqual({
          ...c,
          actual: c.canWrite,
        });
      }
    });
  }
});
```

- [ ] **Step 2: Run — expect parity tests PASS**

```bash
cd /orcd/home/002/yibei/schist/mcp-server
npm run build && npm test -- --testPathPatterns=vault-acl.test 2>&1 | tail -10
```

Expected: 21 prior tests + 1 discovery test + 4 parity tests (one per fixture) = 26 pass.

If a parity test fails, the mismatch is either in `_scope_matches` (Python) vs `scopeMatches` (TS), or the fixture itself disagrees with one of the implementations. Fix the diverging implementation (TS, since Python is the reference).

- [ ] **Step 3: Commit**

```bash
cd /orcd/home/002/yibei/schist
git add mcp-server/tests/vault-acl.test.ts
git commit -m "test(mcp-server): vault-acl parity against shared fixtures (#155)"
```

---

## Task 6: ACL_DENIED error code

**Files:**
- Modify: `mcp-server/src/types.ts`

- [ ] **Step 1: Inspect the existing error-code shape**

Run:

```bash
grep -n "error:\|ToolError\|VALIDATION_ERROR\|GIT_ERROR\|PATH_TRAVERSAL" /orcd/home/002/yibei/schist/mcp-server/src/types.ts | head -10
```

Confirms how error codes are declared (likely a string-literal field, not a closed union). If a closed union, list the existing literals and add `"ACL_DENIED"` to it.

- [ ] **Step 2: Add ACL_DENIED**

Depending on what Step 1 showed, either:

(a) If `ToolError` is `{ error: string; message: string; details?: unknown }` (open shape), no code change is strictly required — `ACL_DENIED` is just a new value of `error`. Add a doc comment block near the type listing all known error codes including the new one, OR

(b) If `ToolError` declares a string literal union (`type ErrorCode = "VALIDATION_ERROR" | "GIT_ERROR" | ...`), extend it to include `"ACL_DENIED"`.

In either case, the diff is small. Pick the form that matches the existing file.

- [ ] **Step 3: Run the typechecker — expect PASS**

```bash
cd /orcd/home/002/yibei/schist/mcp-server
npm run build 2>&1 | tail -5
```

Expected: clean build. If errors, the literal-union case applied but a callsite implicitly narrowed and now needs an `as` cast — fix at the callsite.

- [ ] **Step 4: Commit**

```bash
cd /orcd/home/002/yibei/schist
git add mcp-server/src/types.ts
git commit -m "feat(mcp-server): add ACL_DENIED error code (#155)"
```

---

## Task 7: Enforce ACL in create_note

**Files:**
- Modify: `mcp-server/src/tools.ts`
- Modify: `mcp-server/tests/tools.test.ts`

- [ ] **Step 1: Add the import to tools.ts**

At the top of `mcp-server/src/tools.ts`, after the existing imports from `./types.js`, add:

```typescript
import { loadVaultAcl, canWrite, deriveScope } from "./vault-acl.js";
```

- [ ] **Step 2: Add a helper to write a vault.yaml in the test harness**

In `mcp-server/tests/tools.test.ts`, locate `makeTempVault`. Add a new helper *below* it (do not modify `makeTempVault` itself — it must keep producing vaults WITHOUT vault.yaml so existing tests still skip the ACL check):

```typescript
async function makeTempVaultWithAcl(
  identity: string,
  writeGrants: string[],
  extraYaml = "",
): Promise<string> {
  const vault = await makeTempVault(extraYaml);
  const vaultYaml =
    `vault_version: 1
name: test-acl-vault
scope_convention: flat
participants:
  - name: ${identity}
    type: spoke
    default_scope: global
access:
  ${identity}:
    read: ["*"]
    write: [${writeGrants.join(", ")}]
`;
  await fs.writeFile(path.join(vault, "vault.yaml"), vaultYaml, "utf-8");
  await execFile("git", ["add", "vault.yaml"], { cwd: vault });
  await execFile("git", ["commit", "-m", "add vault.yaml"], { cwd: vault });
  return vault;
}
```

- [ ] **Step 3: Write failing integration tests for create_note ACL enforcement**

Append a new `describe` block near the end of `mcp-server/tests/tools.test.ts`, after the existing `create_note` describe blocks:

```typescript
// ---------------------------------------------------------------------------
// create_note — ACL enforcement against vault.yaml (#155)
// ---------------------------------------------------------------------------

describe("create_note ACL enforcement (#155)", () => {
  test("write to a granted directory succeeds", async () => {
    const vault = await makeTempVaultWithAcl(TEST_AGENT, ["notes"]);
    const config = await loadVaultConfig(vault);
    const result = await create_note(
      vault,
      { owner: TEST_AGENT, title: "Allowed", body: "x", directory: "notes" },
      config,
    ) as { id: string; path: string; commitSha: string };
    expect(result.path).toBeDefined();
  }, 30000);

  test("write to an ungranted directory returns ACL_DENIED", async () => {
    const vault = await makeTempVaultWithAcl(TEST_AGENT, ["notes"]);
    const config = await loadVaultConfig(vault);
    const result = await create_note(
      vault,
      { owner: TEST_AGENT, title: "Denied", body: "x", directory: "papers" },
      config,
    ) as { error: string; message: string };
    expect(result.error).toBe("ACL_DENIED");
    expect(result.message).toMatch(/papers/);
    expect(result.message).toMatch(new RegExp(TEST_AGENT));
  }, 30000);

  test("parent grant covers nested target directory", async () => {
    const vault = await makeTempVaultWithAcl(TEST_AGENT, ["projects"], "  - projects");
    const config = await loadVaultConfig(vault);
    const result = await create_note(
      vault,
      { owner: TEST_AGENT, title: "Nested", body: "x", directory: "projects/foo" },
      config,
    ) as { id: string; path: string };
    expect(result.path?.startsWith("projects/foo/")).toBe(true);
  }, 30000);

  test("identity not in vault.yaml access returns ACL_DENIED", async () => {
    // Vault grants 'other-agent' but TEST_AGENT is unknown to the access map.
    const vault = await makeTempVaultWithAcl("other-agent", ["notes"]);
    const config = await loadVaultConfig(vault);
    const result = await create_note(
      vault,
      { owner: TEST_AGENT, title: "Stranger", body: "x", directory: "notes" },
      config,
    ) as { error: string; message: string };
    expect(result.error).toBe("ACL_DENIED");
  }, 30000);

  test("no vault.yaml → check is skipped, write succeeds", async () => {
    const vault = await makeTempVault();  // no vault.yaml
    const config = await loadVaultConfig(vault);
    const result = await create_note(
      vault,
      { owner: TEST_AGENT, title: "No ACL", body: "x", directory: "notes" },
      config,
    ) as { id: string; path: string };
    expect(result.path).toBeDefined();
  }, 30000);
});
```

- [ ] **Step 4: Run — expect first ACL test to FAIL (no guard yet)**

```bash
cd /orcd/home/002/yibei/schist/mcp-server
npm run build && npm test -- --testPathPatterns=tools.test 2>&1 | tail -15
```

Expected: the "ungranted directory returns ACL_DENIED" and "identity not in vault.yaml" tests fail because there's no guard yet. The "no vault.yaml" and "write to granted dir" tests pass (existing behavior). Parent-grant test passes vacuously.

- [ ] **Step 5: Add the ACL guard in create_note**

In `mcp-server/src/tools.ts`, find the `create_note` body. The right insertion point is *after* `slug` + date-prefix validation, *after* `relPath` is computed (around line 673 in current source), but *before* `writeNote`. Insert:

```typescript
    // #155: intersect with vault.yaml write-grants so we never produce a
    // local commit the hub's pre-receive will reject. Fail-open when
    // vault.yaml is missing or malformed (see loadVaultAcl's comment).
    //
    // PIVOT POINT: if we ever want soft-warn instead of hard-reject
    // (produce the note, attach a warning to the response), flip this
    // early-return into a syncWarning accumulator entry alongside the
    // existing one. One branch to change — keep it that way.
    const acl = loadVaultAcl(vaultRoot);
    if (acl !== null) {
      const scope = deriveScope(relPath);
      if (!canWrite(acl, owner, scope)) {
        return {
          error: "ACL_DENIED",
          message:
            `Identity '${owner}' is not granted write access to scope ` +
            `'${scope}' by vault.yaml. Hub push would reject this write. ` +
            `Ask the hub admin to extend your write grant.`,
        } satisfies ToolError;
      }
    }
```

Place this **after** the same-day collision suffix logic so `relPath` is final. Verify by re-reading lines 666-705 of tools.ts — the guard goes *just* before `const metadata: Record<string, unknown> = { ... }`.

- [ ] **Step 6: Run — expect all pass**

```bash
npm run build && npm test -- --testPathPatterns=tools.test 2>&1 | tail -10
```

Expected: full tools.test.ts suite is green (existing 40 + 5 new = 45 tests).

- [ ] **Step 7: Commit**

```bash
cd /orcd/home/002/yibei/schist
git add mcp-server/src/tools.ts mcp-server/tests/tools.test.ts
git commit -m "feat(mcp-server): create_note rejects writes outside vault.yaml grants (#155)"
```

---

## Task 8: Enforce ACL in add_connection

**Files:**
- Modify: `mcp-server/src/tools.ts`
- Modify: `mcp-server/tests/tools.test.ts`

- [ ] **Step 1: Confirm add_connection's path variable**

`add_connection` lives in `mcp-server/src/tools.ts` starting at line 752. The function takes `args: { owner, source, target, type, context? }`. The vault-relative path is **`args.source`** — that's the file the function reads, modifies, and writes back via `writeNote(vaultRoot, args.source, ...)` at line 786-792. `deriveScope(args.source)` produces the scope.

The guard belongs after the existing `PATH_TRAVERSAL` check (line 763-765) and the `NOT_FOUND` check (line 768-772), before the line-builder at line 774. This ordering matches `create_note`: identity validated → path safety verified → ACL check → mutation.

- [ ] **Step 2: Write a failing test for add_connection ACL**

Append to `mcp-server/tests/tools.test.ts`:

```typescript
describe("add_connection ACL enforcement (#155)", () => {
  test("appending to a note in an ungranted directory returns ACL_DENIED", async () => {
    // Step 1: write a note with 'notes' grant
    const vault = await makeTempVaultWithAcl(TEST_AGENT, ["notes", "papers"]);
    const config = await loadVaultConfig(vault);
    const created = await create_note(
      vault,
      { owner: TEST_AGENT, title: "Target", body: "x", directory: "papers" },
      config,
    ) as { id: string; path: string };
    expect(created.path).toBeDefined();

    // Step 2: rewrite vault.yaml to revoke papers (now only 'notes')
    const tighterYaml =
      `vault_version: 1
name: test-acl-vault
scope_convention: flat
participants:
  - name: ${TEST_AGENT}
    type: spoke
    default_scope: global
access:
  ${TEST_AGENT}:
    read: ["*"]
    write: [notes]
`;
    await fs.writeFile(path.join(vault, "vault.yaml"), tighterYaml, "utf-8");
    await execFile("git", ["add", "vault.yaml"], { cwd: vault });
    await execFile("git", ["commit", "-m", "revoke papers"], { cwd: vault });

    // Step 3: add_connection should now be denied for the papers note
    const result = await add_connection(
      vault,
      { owner: TEST_AGENT, source: created.path, target: "[[Some Concept]]", type: "related" },
    ) as { error: string; message: string };
    expect(result.error).toBe("ACL_DENIED");
    expect(result.message).toMatch(/papers/);
  }, 30000);
});
```

Note: `add_connection`'s real signature is `(vaultRoot, args)` — only two positional args, no `config`. Args are `{ owner, source, target, type, context? }`. Make sure `add_connection` is in the existing import line at the top of `tools.test.ts`.

- [ ] **Step 3: Run — expect FAIL**

```bash
cd /orcd/home/002/yibei/schist/mcp-server
npm run build && npm test -- --testPathPatterns=tools.test 2>&1 | tail -10
```

Expected: the new add_connection ACL test fails (no guard yet).

- [ ] **Step 4: Add the guard in add_connection**

In `mcp-server/src/tools.ts`, find the existing `NOT_FOUND` early-return at line 768-772 (`return { error: "NOT_FOUND", message: \`Source note not found: ${args.source}\` }`). Insert the new ACL guard between that block and the `const conn = { ... }` builder at line 774:

```typescript
    // #155: ACL check — mirror create_note's guard. args.source is the
    // vault-relative path; scope derivation uses the same rule as
    // pre_receive.py:derive_scope on the hub.
    const acl = loadVaultAcl(vaultRoot);
    if (acl !== null) {
      const scope = deriveScope(args.source);
      if (!canWrite(acl, owner, scope)) {
        return {
          error: "ACL_DENIED",
          message:
            `Identity '${owner}' is not granted write access to scope ` +
            `'${scope}' by vault.yaml. Hub push would reject this write. ` +
            `Ask the hub admin to extend your write grant.`,
        } satisfies ToolError;
      }
    }
```

- [ ] **Step 5: Run — expect all pass**

```bash
npm run build && npm test -- --testPathPatterns=tools.test 2>&1 | tail -8
```

Expected: full suite green (45 + 1 new = 46 tests).

- [ ] **Step 6: Commit**

```bash
cd /orcd/home/002/yibei/schist
git add mcp-server/src/tools.ts mcp-server/tests/tools.test.ts
git commit -m "feat(mcp-server): add_connection rejects writes outside vault.yaml grants (#155)"
```

---

## Task 9: Spoke doctor ACL drift check

**Files:**
- Modify: `cli/schist/doctor.py`
- Modify: `cli/tests/test_doctor.py`

- [ ] **Step 1: Inspect existing doctor patterns**

Run:

```bash
grep -n "^def check_\|CheckResult(\|run_doctor" /orcd/home/002/yibei/schist/cli/schist/doctor.py | head -30
```

Note the `CheckResult` signature (status, name, message, fix-hint) so the new check mirrors existing checks' shape.

- [ ] **Step 2: Read check_spoke for the SKIP pattern**

Read `cli/schist/doctor.py` around `def check_spoke(...)` (use the line number found in Step 1). The existing function shows how to detect "not a spoke" and return a SKIP-or-equivalent. Reuse that detection.

- [ ] **Step 3: Write failing tests for check_spoke_acl_drift**

Append to `cli/tests/test_doctor.py`:

```python
from pathlib import Path

import pytest

from schist.doctor import check_spoke_acl_drift


def _write_vault(tmp_path: Path, schist_yaml: str, vault_yaml: str | None,
                 spoke_yaml: str | None) -> Path:
    (tmp_path / "schist.yaml").write_text(schist_yaml)
    if vault_yaml is not None:
        (tmp_path / "vault.yaml").write_text(vault_yaml)
    if spoke_yaml is not None:
        (tmp_path / ".schist").mkdir(exist_ok=True)
        (tmp_path / ".schist" / "spoke.yaml").write_text(spoke_yaml)
    return tmp_path


def test_drift_present_warns(tmp_path: Path) -> None:
    _write_vault(
        tmp_path,
        schist_yaml="directories:\n  notes: notes/\n  papers: papers/\n  logs: logs/\n",
        vault_yaml="""\
vault_version: 1
name: test
scope_convention: flat
participants:
  - name: orcd
    type: spoke
    default_scope: global
access:
  orcd:
    read: ["*"]
    write: [notes, papers]
""",
        spoke_yaml="hub: file:///fake\nidentity: orcd\nscope: global\n",
    )
    result = check_spoke_acl_drift(str(tmp_path))
    assert result.status == "WARN"
    assert "logs" in result.message
    assert "orcd" in result.message


def test_no_drift_passes(tmp_path: Path) -> None:
    _write_vault(
        tmp_path,
        schist_yaml="directories:\n  notes: notes/\n",
        vault_yaml="""\
vault_version: 1
name: test
scope_convention: flat
participants:
  - name: orcd
    type: spoke
    default_scope: global
access:
  orcd:
    read: ["*"]
    write: [notes]
""",
        spoke_yaml="hub: file:///fake\nidentity: orcd\nscope: global\n",
    )
    result = check_spoke_acl_drift(str(tmp_path))
    assert result.status == "PASS"


def test_no_vault_yaml_skips(tmp_path: Path) -> None:
    _write_vault(
        tmp_path,
        schist_yaml="directories:\n  notes: notes/\n",
        vault_yaml=None,
        spoke_yaml="hub: file:///fake\nidentity: orcd\nscope: global\n",
    )
    result = check_spoke_acl_drift(str(tmp_path))
    assert result.status == "SKIP"


def test_not_a_spoke_skips(tmp_path: Path) -> None:
    _write_vault(
        tmp_path,
        schist_yaml="directories:\n  notes: notes/\n",
        vault_yaml="""\
vault_version: 1
name: standalone
scope_convention: flat
participants:
  - name: local
    type: agent
    default_scope: global
access:
  local:
    read: ["*"]
    write: ["*"]
""",
        spoke_yaml=None,  # not a spoke
    )
    result = check_spoke_acl_drift(str(tmp_path))
    assert result.status == "SKIP"


def test_wildcard_grant_passes(tmp_path: Path) -> None:
    _write_vault(
        tmp_path,
        schist_yaml="directories:\n  notes: notes/\n  logs: logs/\n",
        vault_yaml="""\
vault_version: 1
name: test
scope_convention: flat
participants:
  - name: admin
    type: spoke
    default_scope: global
access:
  admin:
    read: ["*"]
    write: ["*"]
""",
        spoke_yaml="hub: file:///fake\nidentity: admin\nscope: global\n",
    )
    result = check_spoke_acl_drift(str(tmp_path))
    assert result.status == "PASS"
```

- [ ] **Step 4: Run — expect FAIL (ImportError on check_spoke_acl_drift)**

```bash
cd /orcd/home/002/yibei/schist
uv run --with pytest --with ./cli python -m pytest cli/tests/test_doctor.py -v -k "spoke_acl_drift or wildcard_grant"
```

Expected: ImportError or AttributeError because the function doesn't exist.

- [ ] **Step 5: Implement check_spoke_acl_drift**

In `cli/schist/doctor.py`, after `check_spoke`, add:

```python
def check_spoke_acl_drift(vault_path: Optional[str]) -> CheckResult:
    """Flag schist.yaml directories not present in this spoke's hub write grant.

    Runs only on spokes (skips standalone vaults and contexts without spoke.yaml).
    Reads schist.yaml directories, the spoke's identity from .schist/spoke.yaml,
    and the per-identity write grant from vault.yaml's access map. Reports any
    schema dir that doesn't match any of the identity's write scopes (using the
    same parent->child rule as the hub's pre-receive).
    """
    name = "Spoke ACL"

    if not vault_path:
        return CheckResult("SKIP", name, "no vault path supplied")

    vault = Path(vault_path)
    spoke_yaml = vault / ".schist" / "spoke.yaml"
    if not spoke_yaml.exists():
        return CheckResult("SKIP", name, "not a spoke")

    vault_yaml = vault / "vault.yaml"
    if not vault_yaml.exists():
        return CheckResult("SKIP", name, "no vault.yaml")

    # Identity from spoke.yaml
    try:
        from schist.spoke_config import SpokeConfig
        spoke = SpokeConfig.read(vault)
        identity = spoke.identity
    except Exception as e:  # noqa: BLE001 — surface as SKIP so doctor never crashes
        return CheckResult("SKIP", name, f"could not read spoke.yaml: {e}")

    # Schema dirs from schist.yaml — reuse existing canonical resolver
    from schist.commands import _load_schist_yaml  # if it exists; otherwise inline read
    # If _load_schist_yaml doesn't exist, fall back to:
    import yaml as _yaml
    try:
        schist_data = _yaml.safe_load((vault / "schist.yaml").read_text()) or {}
    except Exception as e:  # noqa: BLE001
        return CheckResult("SKIP", name, f"could not read schist.yaml: {e}")
    dirs_field = schist_data.get("directories") or {}
    # `directories:` can be either a dict (canonical default.yaml form) or a list (some test fixtures).
    if isinstance(dirs_field, dict):
        schema_dirs = [v.rstrip("/") for v in dirs_field.values()]
    elif isinstance(dirs_field, list):
        schema_dirs = [str(v).rstrip("/") for v in dirs_field]
    else:
        return CheckResult("SKIP", name, "schist.yaml 'directories' field is malformed")

    if not schema_dirs:
        return CheckResult("SKIP", name, "schist.yaml has no directories declared")

    # Parse vault.yaml and resolve the identity's write grant
    try:
        from schist.acl import parse_vault_yaml
        acl = parse_vault_yaml(vault_yaml)
    except Exception as e:  # noqa: BLE001
        return CheckResult("SKIP", name, f"could not parse vault.yaml: {e}")

    entry = acl.access.get(identity)
    if entry is None:
        return CheckResult(
            "WARN", name,
            f"identity '{identity}' has no access entry in vault.yaml — ask the hub admin to add one",
        )

    # Find schema dirs the identity is NOT granted write on.
    from schist.acl import _scope_matches
    drift = [d for d in schema_dirs if not _scope_matches(entry.write, d)]
    if not drift:
        return CheckResult("PASS", name, f"identity '{identity}' is granted all schema directories")

    return CheckResult(
        "WARN", name,
        f"identity '{identity}' has no hub write grant for: {', '.join(drift)}. "
        f"Ask the hub admin to extend your write scope in vault.yaml.",
    )
```

Adjust the `_load_schist_yaml` import to match what actually exists in commands.py — if it doesn't, the inline yaml.safe_load fallback above is correct as-is and the import line can be removed.

- [ ] **Step 6: Run — expect tests PASS**

```bash
cd /orcd/home/002/yibei/schist
uv run --with pytest --with ./cli python -m pytest cli/tests/test_doctor.py -v -k "spoke_acl_drift or wildcard_grant"
```

Expected: all 5 new tests pass.

- [ ] **Step 7: Wire check_spoke_acl_drift into run_doctor**

In `cli/schist/doctor.py`, find `run_doctor`. After the existing `check_spoke(vault_path),` entry in the checks list, add:

```python
        check_spoke_acl_drift(vault_path),
```

- [ ] **Step 8: Run the full doctor test file — expect PASS**

```bash
cd /orcd/home/002/yibei/schist
uv run --with pytest --with ./cli python -m pytest cli/tests/test_doctor.py -v
```

Expected: all pre-existing tests still pass; 5 new pass.

- [ ] **Step 9: Commit**

```bash
cd /orcd/home/002/yibei/schist
git add cli/schist/doctor.py cli/tests/test_doctor.py
git commit -m "feat(cli): doctor check_spoke_acl_drift surfaces schema↔vault.yaml drift (#155)"
```

---

## Task 10: End-to-end verification + PR

**Files:** none modified

- [ ] **Step 1: Run the full MCP TS test suite**

```bash
cd /orcd/home/002/yibei/schist/mcp-server
npm run build 2>&1 | tail -5
npm test -- --testPathPatterns="tools.test|vault-acl.test" 2>&1 | tail -8
```

Expected: clean build; tools.test + vault-acl.test both green. The full `npm test` will show the ~164 pre-existing `better-sqlite3` failures on HPC — that's environment-only and noted in [[hpc-mcp-server-npm-build]]; only the two target suites need to pass locally.

- [ ] **Step 2: Run the full CLI Python test suite**

```bash
cd /orcd/home/002/yibei/schist
uv run --with pytest --with ./cli python -m pytest cli/tests/ -v 2>&1 | tail -20
```

Expected: all pass, including the new parity + doctor tests.

- [ ] **Step 3: Push the branch**

```bash
git push -u origin feat/155-mcp-hub-acl-intersection 2>&1 | tail -5
```

- [ ] **Step 4: Open the PR**

```bash
gh pr create --title "feat(mcp-server): intersect create_note/add_connection with vault.yaml ACL (#155)" --body "$(cat <<'EOF'
## Summary
- Closes #155 (MCP↔hub-ACL intersection). #154 (admin + grant CLI) deferred to a follow-up brainstorming session — see the spec for rationale.
- New \`mcp-server/src/vault-acl.ts\` ports the read-side of \`cli/schist/acl.py\` (~80 LoC). Both vault-write tools (\`create_note\`, \`add_connection\`) now reject with \`ACL_DENIED\` when the calling identity's \`vault.yaml\` grants don't cover the target scope.
- Missing or malformed \`vault.yaml\` → log warning, skip the check (fail-open). Hub pre-receive remains the trust boundary.
- Shared parity fixtures at \`cli/schist/acl-fixtures/\` (YAML + cases.json) loaded by both Python and TS test suites — adding an ACL rule means adding a fixture, not editing two parallel test files.
- New \`schist doctor\` check (\`check_spoke_acl_drift\`) surfaces schema↔vault.yaml drift on the spoke with an actionable hint.
- Three explicit pivot-point comments in code mark future-malleability axes (missing-file fail-open, malformed fail-open, hard-reject branch).

## Spec
\`docs/superpowers/specs/2026-05-28-mcp-hub-acl-intersection-design.md\`

## Test plan
- [x] vault-acl.test.ts: 26/26 (parser, canWrite, scopeMatches, deriveScope, parity)
- [x] tools.test.ts: 46/46 (existing 40 + 5 create_note ACL + 1 add_connection ACL)
- [x] test_acl_parity.py: 4/4 fixture pairs
- [x] test_doctor.py: existing + 5 new for check_spoke_acl_drift
- [x] Wheel ships acl-fixtures/ (verified via \`uv build --wheel\`)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Watch CI**

```bash
sleep 5
gh pr checks --watch --interval 15 2>&1 | tail -10
```

Expected: CLI (Python), MCP server (TypeScript), GitGuardian → all pass. Auto-merge → skipping (expected; this is a Yibei-authored PR, not a Dependabot one).

- [ ] **Step 6: Run /review (skill) on the PR**

Manually invoke `/review` to get a structured pre-landing check. Address any AUTO-FIX / ASK findings. The diff will be ~300-450 lines (medium tier per the skill), so the medium adversarial pass via Claude subagent will fire (Codex is unavailable on HPC per the earlier session note).

- [ ] **Step 7: Squash-merge when clean**

```bash
git checkout main
gh pr merge --squash --delete-branch
git pull --ff-only
```

---

## Spec coverage self-check

| Spec section | Plan task |
|---|---|
| New `vault-acl.ts` (parseVaultAcl, canWrite, scopeMatches, deriveScope) | Tasks 2, 3, 4 |
| `tools.ts` guard in `create_note` + `add_connection` | Tasks 7, 8 |
| `ACL_DENIED` error code | Task 6 |
| `vault-acl.test.ts` unit tests (parser, canWrite, scopeMatches, deriveScope) | Tasks 2, 3, 4 |
| Shared parity fixtures at `cli/schist/acl-fixtures/` | Task 1 |
| Python parity loader in `cli/tests/test_acl_parity.py` | Task 1 |
| TS parity loader in `vault-acl.test.ts` | Task 5 |
| Integration tests in `tools.test.ts` (5 cases) | Task 7 (5 cases) + Task 8 (add_connection case) |
| `check_spoke_acl_drift` in `doctor.py` + tests | Task 9 |
| Wheel ships acl-fixtures via `pyproject.toml` | Task 1 Step 8-9 |
| Three pivot-point comments | Task 3 (fail-open comments inside `loadVaultAcl`), Task 7 (hard-reject pivot comment in `create_note` guard) |

All spec sections have a corresponding task.

## Out of scope (per spec — do NOT do in this PR)

- Hub admin participant concept
- `schist hub grant/revoke/participant add` CLI
- Hub-side doctor check
- Migration for existing hubs without an admin
- Deriving the hub seed write list from `default.yaml`
