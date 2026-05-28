# MCP↔hub-ACL intersection (PR1 of #155 + #154 pair) — design spec

**Date:** 2026-05-28
**Closes:** #155 (scope of this PR)
**Forward-references:** #154 (admin participant + `schist hub grant` CLI — PR2 in a follow-up session)
**PR shape:** 1 PR — MCP ACL check + spoke-side doctor drift detection. Hub admin concept and grant CLI deferred to PR2.

## Context

The MCP server's `create_note` validates the target directory against `schist.yaml`'s `directories:` list (the 8 canonical content-axis dirs from `cli/schist/default.yaml`). It does **not** consult `vault.yaml`'s per-participant `access.<id>.write` grants.

On a spoke whose hub grants a narrower write list (the default hub seed grants 6 dirs — `research, concepts, decisions, notes, ops, papers` — and excludes `logs/` and `projects/`), an agent can call `create_note --directory logs`. It succeeds locally and creates a git commit, but the hub's `pre-receive` hook rejects the push as out-of-scope. The result is a stuck, un-pushable local commit with no clear signal to the agent.

This mismatch is **pre-existing** — the MCP server has never read `vault.yaml` — and was widened by #153 (flatten-spoke-dirs) when the schema directory list grew from 3 to 8 dirs while the hub seed's `write:` list stayed at 6. Surfaced by the adversarial `/review` pass on #153 and deferred out of scope at the time.

## Why phased (PR1 first)

`#154` requires an admin participant with `*` write to land a CLI like `schist hub grant`, because the hub's pre-receive enforces `*` write for any root-file change (including `vault.yaml`). The current `_build_seed_vault` (`cli/schist/sync.py:633`) does not create an admin. Adding one is a design choice with backward-compat implications (existing hubs lack an admin) that deserves its own brainstorming pass.

PR1 (this spec) closes the user-visible bug — stuck local commits — without taking on the admin design. The error message produced by PR1 forward-references the future `schist hub grant` CLI, which is harmless when #154 lands later.

## Design decisions (interview record)

1. **Sequencing.** Phased: PR1 closes #155 only (MCP enforcement + spoke doctor check). PR2 addresses #154 (admin concept, grant CLI, hub doctor).
2. **ACL check location.** Port a minimal read-side of `cli/schist/acl.py` to TypeScript. No subprocess to Python on every write; no new IPC; `js-yaml` is already an MCP dep.
3. **Surface area.** Only `parseVaultAcl`, `canWrite`, `scopeMatches` are ported. Validation logic stays in Python — the hub remains the single source of truth for vault.yaml validity. Malformed `vault.yaml` on the spoke logs a warning and skips the check (fail-open). The hub will still reject any push that fails validation on its side.
4. **Behavior on missing vault.yaml.** Skip the ACL check entirely. Standalone vaults, pre-vault.yaml deployments, and existing MCP test fixtures continue to work unchanged.
5. **Behavior on unknown identity.** Reject (`ACL_DENIED`). Matches `pre_receive.py:check_push` behavior — an identity not in `vault.yaml.access` has no write rights.
6. **Tools covered.** Both `create_note` and `add_connection`. The latter modifies an existing note's path; same scope-derivation rule applies, same hub pre-receive would reject.
7. **Parity strategy.** Shared fixtures at `cli/schist/acl-fixtures/` (YAML inputs + JSON expected outcomes) loaded by both `cli/tests/test_acl.py` and `mcp-server/tests/vault-acl.test.ts`. The contract is data, not code — either implementation can be replaced later without touching the tests.
8. **No caching.** Parsing a small `vault.yaml` per write is sub-millisecond; mtime-tracked caching adds complexity for negligible gain. Revisit only on measured perf data.
9. **New error code.** `ACL_DENIED` in `mcp-server/src/types.ts`. The message includes identity, scope, and a hint pointing at the future `schist hub grant` CLI.
10. **Long-term malleability.** Three explicit pivot points are marked in code comments so future requirements changes are local: (a) the missing-vault.yaml fail-open branch, (b) the malformed-vault.yaml fail-open warning, (c) the hard-reject early-return (one branch to flip if we ever want soft-warn). All asymmetric-vs-rate_limit.py choices are commented with the rationale.

## Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│ Agent calls create_note(owner, title, directory="logs", body=…)        │
└────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│ mcp-server/src/tools.ts:create_note                                    │
│                                                                        │
│   validateOwner(owner)              ── existing (#63)                  │
│   directory ∈ schist.yaml dirs?     ── existing                        │
│   slugify + date-prefix guard       ── existing (#118)                 │
│                                                                        │
│   relPath = `${directory}/${date}-${slug}.md`                          │
│   scope   = deriveScope(relPath)    ── NEW (mirrors pre_receive.py)    │
│                                                                        │
│   loadVaultAcl(vaultRoot)           ── NEW                             │
│     missing  → return null (skip)                                      │
│     malformed → warn + return null  (skip)                             │
│                                                                        │
│   acl?.canWrite(owner, scope)       ── NEW                             │
│     false   → return { error: "ACL_DENIED", ... }                      │
│     true / null  → proceed                                             │
│                                                                        │
│   writeNote(...)                    ── existing                        │
└────────────────────────────────────────────────────────────────────────┘
```

`add_connection` follows the same shape, with `scope = deriveScope(<target-note-path>)`.

## Components

| File | Status | Role |
|---|---|---|
| `mcp-server/src/vault-acl.ts` | new (~80 lines) | `loadVaultAcl(root)`, `canWrite(identity, scope)`, `scopeMatches(allowed[], target)`, `deriveScope(filepath)` |
| `mcp-server/src/tools.ts` | edit | `create_note` and `add_connection` gain the ACL guard between existing validation and `writeNote` |
| `mcp-server/src/types.ts` | edit | Add `ACL_DENIED` to the error-code union |
| `mcp-server/tests/vault-acl.test.ts` | new | Unit tests for parser, `canWrite`, `scopeMatches`, `deriveScope`; missing-file + malformed-file paths |
| `mcp-server/tests/tools.test.ts` | edit | Integration tests: grant present (allowed), grant absent (denied), unknown identity (denied), missing vault.yaml (allowed), `add_connection` (denied) |
| `cli/schist/acl-fixtures/` | new | Shared YAML+JSON parity fixtures |
| `cli/tests/test_acl.py` | edit | Load parity fixtures; assert `can_write` matches |
| `cli/tests/test_vault_acl_parity.py` | new (optional) | Dedicated parity test runner (Python side) if the existing test_acl.py grows too noisy |
| `cli/schist/doctor.py` | edit | New `check_spoke_acl_drift(vault_path)` |
| `cli/tests/test_doctor.py` | edit | Cases for `check_spoke_acl_drift`: drift present → WARN with hint, no drift → PASS, no vault.yaml → skip |
| `mcp-server/src/protocol/` | no change | Cursor protocol untouched |

## Data flow & contracts

### `deriveScope(filepath: string): string`

Mirrors `cli/schist/pre_receive.py:derive_scope` verbatim. Normalize the path with `path.posix.normalize`, take the directory portion. Root-level files → empty string (caller decides what to do with empty scope — for `create_note`/`add_connection`, this never happens because both tools always write under a directory; but the function handles it for symmetry).

| Input | Output |
|---|---|
| `notes/2026-05-28-foo.md` | `notes` |
| `projects/brain-states-friends/2026-05-28-bar.md` | `projects/brain-states-friends` |
| `vault.yaml` | `""` (root) |
| `../etc/passwd` | normalized to `../etc` — handled by the upstream `assertPathSafe` guard, not the ACL check |

### `canWrite(identity: string, scope: string): boolean`

Mirrors `cli/schist/acl.py:VaultACL.can_write`:

```
if identity not in acl.access: return false
return scopeMatches(acl.access[identity].write, scope)
```

### `scopeMatches(allowed: string[], target: string): boolean`

Mirrors `_scope_matches`:
- `"*"` in `allowed` → `true`
- exact match → `true`
- `target.startsWith(scope + "/")` (parent grants child) → `true`
- else `false`

### Error response

```ts
{
  error: "ACL_DENIED",
  message:
    `Identity '${owner}' is not granted write access to scope '${scope}' ` +
    `by vault.yaml. Hub push would reject this write. ` +
    `Ask the hub admin to extend your write grant.`
}
```

(The trailing hint will say `schist hub grant` once #154 lands; written as a generic ask for now to avoid forward-referencing a command that doesn't exist yet.)

## Behavior matrix

| Condition | Behavior |
|---|---|
| `vault.yaml` missing | Skip check; proceed with write (backward compat) |
| `vault.yaml` malformed YAML | Log warning; skip check; proceed with write (fail-open) |
| `vault.yaml` valid but no `access` entry for identity | `ACL_DENIED` |
| Identity granted scope exactly | Allowed |
| Identity granted parent scope (e.g. `projects` vs target `projects/foo`) | Allowed |
| Identity granted `"*"` | Allowed |
| Identity granted some scopes but not the target | `ACL_DENIED` |
| Target scope is root (empty string) and identity lacks `"*"` | `ACL_DENIED` (consistent with `pre_receive.py:118-127`) |

## Spoke doctor drift check

New check in `cli/schist/doctor.py`:

```python
def check_spoke_acl_drift(vault_path: Optional[str]) -> CheckResult:
    """Flag schist.yaml directories not present in this spoke's hub write grant.

    Runs only on spokes (skips standalone / hub-only contexts). Reads:
      - schist.yaml directories  (schema-side dir list)
      - .schist/spoke.yaml identity  (who am I)
      - vault.yaml access.<identity>.write  (what the hub grants me)

    Reports each dir present in the first but uncovered by scope-match against
    the third. WARN, not FAIL — a missing grant is a configuration drift the
    user resolves with the hub admin, not a local error.
    """
```

Output format (drift present):
```
[WARN] Spoke ACL drift: identity 'orcd' has no hub write grant for:
         - logs
         - projects
       Schema dirs (schist.yaml) the hub hasn't granted you write access to.
       Ask the hub admin to extend your grant in vault.yaml.
```

Output format (no drift): `[PASS] Spoke ACL: identity 'orcd' is granted all schema directories.`

Output format (missing vault.yaml or not a spoke): `[SKIP] Spoke ACL: not a spoke / no vault.yaml`

Wired into `run_doctor` after `check_spoke`.

## Parity fixtures

Shared location: `cli/schist/acl-fixtures/` (inside the Python package so it's deployed with the wheel; the TS side reads via a relative path from `mcp-server/dist/` similar to how `default.yaml` is loaded).

Each case is a pair:

```
cli/schist/acl-fixtures/
  basic-write-grant.yaml      # a small vault.yaml
  basic-write-grant.cases.json
  wildcard-write.yaml
  wildcard-write.cases.json
  parent-grants-child.yaml
  parent-grants-child.cases.json
  unknown-identity.yaml
  unknown-identity.cases.json
  empty-scope.yaml
  empty-scope.cases.json
```

Format of `.cases.json`:

```json
[
  { "identity": "orcd", "scope": "research", "canWrite": true },
  { "identity": "orcd", "scope": "logs", "canWrite": false },
  { "identity": "orcd", "scope": "projects/foo", "canWrite": true },
  { "identity": "unknown", "scope": "research", "canWrite": false }
]
```

Loaders:
- Python: `cli/tests/test_acl.py` iterates the directory, loads each pair, asserts `VaultACL.can_write(identity, scope) == expected`.
- TypeScript: `mcp-server/tests/vault-acl.test.ts` does the same against the TS port.

Adding a new ACL rule (e.g. a future wildcard syntax) means adding a fixture pair; both sides discover it automatically.

## Long-term malleability — explicit pivot points

Three code comments are required, each calling out a future-change axis so a later reader knows it's a deliberate choice, not an oversight:

1. **Missing `vault.yaml` fail-open** (in `loadVaultAcl`):
   ```ts
   // Backward-compat: standalone vaults and pre-vault.yaml deployments lack
   // this file. Returning null short-circuits the ACL check at the call site.
   // If vault.yaml is ever made mandatory (e.g., schist v0.3 invariant),
   // remove this branch and let the caller throw.
   ```

2. **Malformed `vault.yaml` fail-open** (in `loadVaultAcl` catch block):
   ```ts
   // Asymmetric with cli/schist/rate_limit.py (fail-closed) by design: the
   // MCP-side check is a UX optimization to fail fast locally; the hub's
   // pre-receive is the security boundary. Logging + skipping here matches
   // tools.ts:loadCanonicalDirectories' fail-open posture.
   ```

3. **Hard-reject branch** (in `create_note` / `add_connection`):
   ```ts
   // If we ever want soft-warn instead of hard-reject (produce the note,
   // attach a warning to the response), flip this early-return into a
   // `syncWarning` accumulator entry alongside the existing one. One branch
   // to change — keep it that way.
   ```

## Testing

### TS unit tests (`mcp-server/tests/vault-acl.test.ts`)

- `parseVaultAcl`: valid, missing file, malformed YAML, missing `access` block.
- `scopeMatches`: exact match, parent-grants-child, wildcard, no match, empty `allowed` array.
- `canWrite`: known identity + granted scope, known identity + ungranted scope, unknown identity, wildcard identity.
- `deriveScope`: top-level dir, nested dir, root file, path with `..` (relies on caller's `assertPathSafe`, but verify the function itself doesn't crash).

### Parity test (both sides)

Iterate `cli/schist/acl-fixtures/*.cases.json`; for each case, assert `canWrite == expected` on both implementations.

### Integration tests (`mcp-server/tests/tools.test.ts`)

Add a new `describe("create_note ACL enforcement", ...)`:

1. Grant present → `create_note` succeeds.
2. Grant absent → `create_note` returns `ACL_DENIED`.
3. Identity unknown to vault.yaml → `ACL_DENIED`.
4. No `vault.yaml` → `create_note` succeeds (existing fixtures untouched).
5. Parent grant covers child scope → succeeds with nested directory like `projects/foo`.
6. `add_connection`: same matrix, abbreviated to the rejection case.

### Doctor test (`cli/tests/test_doctor.py`)

- Drift present → WARN.
- No drift → PASS.
- No vault.yaml → SKIP.
- Standalone vault (vault.yaml present but no `.schist/spoke.yaml`) → SKIP.

## Out of scope (deferred to PR2 / #154)

- Hub admin participant concept
- `schist hub grant/revoke/participant add/remove/rename` CLI
- Hub-side doctor drift check (operator-facing)
- Migration path for existing hubs to add an admin
- Updating the hub seed to derive write grants from `default.yaml`'s directory list rather than the hardcoded 6-dir slice

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| TS/Python ACL logic drifts as `acl.py` evolves | Parity fixtures must run on every PR that touches either side. CI runs both implementations against the same fixtures. |
| Stale spoke `vault.yaml` rejects a write the (updated) hub would now allow | User runs `git pull` and retries. Error message hints at the hub admin — natural prompt to confirm grants. `maybeSpokePull` already runs before `get_context` if the user calls it. |
| Test fixtures without `vault.yaml` break under enforcement | Behavior matrix says missing vault.yaml skips the check. Every existing test fixture works unchanged. |
| Fail-open on malformed vault.yaml means MCP doesn't catch what hub would | Acceptable: hub remains the trust boundary. A `console.warn` makes the failure visible to operators. |
| Path-traversal in `directory` argument | Pre-existing `assertPathSafe` guard catches `..` and absolute paths upstream of `deriveScope`; nothing new to add. |
| `ACL_DENIED` error code addition breaks callers | New error codes are additive in this codebase; no caller asserts a closed union. Verified via grep of `error: "` patterns. |

## Migration

None for existing deployments. Vaults without `vault.yaml` (standalone, pre-existing) continue to work. Vaults with `vault.yaml` start getting the check; identities that were silently producing un-pushable commits now get a clear local error instead. The fix path for the user is the same as before — ask the hub admin to extend the grant — but they discover it before the stuck commit, not after.

## Sequel: PR2 (#154) — preview, not a commitment

PR2 will likely need its own brainstorming session because the design touches:
- Whether `init --hub` gains an `--admin <name>` flag and what default behavior is.
- How the `schist hub grant` CLI runs: from a spoke checkout (clone-edit-push, needs admin grant), from the hub box (needs git plumbing on a bare repo), or both.
- Backward compat for existing hubs without an admin — opt-in migration vs documented manual path.
- Whether the hub seed write-grant list should derive from `default.yaml` directly (eliminating the manual hardcoded 6-dir slice).

These are all live questions left for PR2.
