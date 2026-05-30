# MCP ↔ hub ACL: close the `participants` parity gap (#160)

**Date:** 2026-05-30
**Issue:** #160 — MCP `loadVaultAcl` fail-opens on hub-invalid `vault.yaml` (structural validation gap)
**Related:** #155 (MCP↔hub-ACL intersection), #154 (hub admin CLI), `docs/superpowers/specs/2026-05-28-mcp-hub-acl-intersection-design.md`

## Problem

`mcp-server/src/vault-acl.ts:loadVaultAcl` is a loose parser: it builds an
`access` map from `vault.yaml` and returns it, falling open (returning `null`,
so the caller skips the local ACL check) only on three conditions — missing
file, unparseable YAML, or a missing `access` mapping.

The hub's strict parser `cli/schist/acl.py:parse_vault_yaml` rejects a broader
set of structural problems by raising `ACLError`, and the hub's pre-receive
fail-closes on them. So a `vault.yaml` that is **structurally invalid to the
hub** but parses as valid-enough YAML for the TS reader can let the MCP-side
check return `canWrite = true` for a write the hub will later reject — the exact
stuck-commit symptom #155 set out to prevent.

The two divergences that can produce a **false grant** (TS allows, hub rejects):

1. `participants:` missing or not a non-empty list (acl.py:145–146). TS ignores
   `participants` entirely, so it still enforces `access` as if valid.
2. An identity present in `access:` but absent from `participants:`
   (acl.py:233–235). TS admits that identity into its `access` map, so
   `canWrite` can return `true` for it.

Every *other* acl.py reject path (participant with no access entry, empty
`read`/`write` list, invalid scope syntax, bad participant-name regex) fails in
the **harmless over-deny direction** on the TS side: the loose parse yields a
`canWrite = false`, i.e. a denial, never a false grant. The issue itself notes
this. Under fail-open semantics (below) only false grants matter, so only the
two `participants` invariants need mirroring.

## Severity

Low — the trigger is largely unreachable in normal operation. A spoke's
`vault.yaml` is pulled *from* the hub, the hub validates its own `vault.yaml`
with the strict parser at `schist init --hub` and rejects any pushed
`vault.yaml` that fails validation. Hitting this requires hand-editing a
spoke's local `vault.yaml` into an invalid state (operator error). This fix
hardens parity and removes a confusing half-enforced-ACL failure mode; it is
not closing a live exploit.

## Decision: fail-open, not fail-closed

When the TS parser detects a structurally-invalid `vault.yaml`, `loadVaultAcl`
returns `null` (fail-open) — the caller skips the local ACL check and the write
proceeds; the hub's pre-receive remains the trust boundary. This matches the
documented #155 posture (MCP is a UX fast-fail; the hub is the gate) and
carries no false-denial risk.

This deliberately does **not** change the stuck-commit outcome for the
`participants` cases: a write against an invalid local `vault.yaml` still
proceeds locally and is still rejected at hub push. What it *does* fix is the
parser's **honesty** — today a structurally-broken file is half-enforced,
producing both spurious grants and spurious denials. After this change the TS
parser recognizes the same write-path-critical invalid set the Python parser
does and falls open consistently on it, instead of enforcing a garbled ACL.

The alternative — fail-closed (return a new `INVALID_VAULT_ACL` denial before
committing) — would prevent the stuck commit but diverges from the #155 posture
and, because the TS parser mirrors only a subset of acl.py, risks false-positive
denials. Rejected.

## Design

### Section 1 — TS parser change (`mcp-server/src/vault-acl.ts`)

After `loadVaultAcl` builds the `access` map, add two validation gates that
mirror acl.py's write-path invariants. On failure: `console.warn` + `return
null` (fail-open, identical mechanism to the existing missing-`access` branch).

1. **`participants` must be a non-empty array** (acl.py:145–146). Read
   `raw.participants`; if it is not an array or is empty → warn + `return null`.
2. **Every `access` key must be a participant name** (acl.py:233–235). Build the
   set of participant names from the `participants` list (each entry may be a
   string or a `{name: ...}` mapping, matching acl.py's two accepted shapes); if
   any `access` key is not in that set → warn + `return null`.

Participant-name extraction mirrors acl.py's tolerance: a `participants` entry
that is a bare string is its own name; an entry that is a mapping uses its
`name` field; any other shape contributes no name (so an `access` key can never
match it, which correctly drives a reject). No change to `canWrite`,
`scopeMatches`, or `deriveScope`. No caller change in `tools.ts` (`null` already
means "skip"). The module doc comment gains a documented 4th/5th fail-open case.

### Section 2 — fixture format & harness

Reject fixtures reuse the existing `<name>.yaml` + `<name>.cases.json` pair
convention, distinguished by the **shape** of the `.cases.json`:

- **Accept fixture** (existing): `.cases.json` is a JSON **array** of
  `{identity, scope, canWrite}` → both harnesses parse the file (expect
  non-null) and assert `can_write`/`canWrite` per case.
- **Reject fixture** (new): `.cases.json` is a JSON **object**
  `{"reject": true, "reason": "<human-readable why>"}` → the Python harness
  asserts `parse_vault_yaml` raises `ACLError`; the TS harness asserts
  `loadVaultAcl` returns `null`.

New reject fixtures:

1. `reject-missing-participants.{yaml,cases.json}` — a valid `access` block, no
   `participants:` key. Exercises gate #1 / acl.py:146.
2. `reject-access-not-participant.{yaml,cases.json}` — `participants: [alice]`
   but an `access` block keyed on `bob`. Exercises gate #2 / acl.py:235.

Harness edits — both branch on the case-file shape (`Array.isArray` /
`isinstance(..., list)`):

- `cli/tests/test_acl_parity.py` — load `cases.json`; if it is a dict with
  `reject: true`, assert `with pytest.raises(ACLError): parse_vault_yaml(path)`;
  else run the existing per-case `can_write` loop.
- `mcp-server/tests/vault-acl.test.ts` — in the parity loop, if the parsed cases
  object has `reject === true`, write the fixture to a temp vault and
  `expect(loadVaultAcl(dir)).toBeNull()`; else run the existing per-case
  `canWrite` loop.
- `cli/schist/acl-fixtures/README.md` — document both `.cases.json` shapes.

The `expect(yamlFiles.length).toBeGreaterThanOrEqual(4)` sanity check in the TS
harness still holds (we add fixtures, never remove).

## Testing

TDD order:

1. **RED** — add the two reject fixtures and the harness branching. The Python
   branch goes green immediately (`parse_vault_yaml` already raises today). The
   TS branch goes RED: the gates don't exist yet, so `loadVaultAcl` returns a
   non-null ACL and `toBeNull()` fails.
2. **GREEN** — add the two TS validation gates; the TS reject-fixture assertions
   pass.

Full verification:

- `cd mcp-server && npm test` — full Jest suite (parity + unit gates).
- `python -m pytest cli/tests/test_acl_parity.py` — Python parity branch.
- `python -m pytest cli/tests/` — confirm no acl.py regressions.

## Out of scope

- Mirroring the over-deny acl.py reject paths (empty `read`/`write`, scope
  syntax, participant-name regex). These fail safely on the TS side and add
  parity surface (drift risk) for no false-grant benefit. YAGNI.
- Any change to fail-open vs fail-closed posture. Settled above.
- Caller/`tools.ts` changes. `null` already means skip.
