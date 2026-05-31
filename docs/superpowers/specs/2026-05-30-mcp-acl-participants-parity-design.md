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

### Which reject paths are false grants

`parse_vault_data` (acl.py:144–331) **accumulates** errors in a list and raises
`ACLError` once at the end (acl.py:330–331), with two early hard-raises for the
structural prerequisites (`participants` non-empty list at :184–185; `access`
non-empty mapping at :260–261). Critically, an accumulated error does **not**
stop the parser from appending the offending participant (acl.py:250) or
populating the `access` entry (acl.py:301). So for almost every participant- or
access-level error, Python rejects the whole file while the loose TS parser
still returns a non-null ACL whose `canWrite` can be `true` — a **false grant**
(TS allows, hub rejects → stuck commit).

The false-grant paths are therefore far broader than the two originally
identified. Classifying every reject path in `parse_vault_data`:

| acl.py reject path | line | TS today | false grant? |
|---|---|---|---|
| `participants` missing / not non-empty list | 184 | enforces `access` anyway | **yes** |
| participant entry not string/mapping | 193 | identity absent → over-deny | no¹ |
| participant `name` missing / non-string | 198 | identity absent → over-deny | no¹ |
| participant `name` fails `^[a-z][a-z0-9-]*$` | 201 | name still admitted | **yes** |
| duplicate participant name | 207 | name still admitted | **yes** |
| access key ∉ participants | 273 | identity admitted | **yes** |
| participant ∉ access (no access entry) | 268 | *other* grants admitted | **yes**² |
| participant `type`/`transport`/`default_scope`/`metadata` invalid | 211–248 | grant still admitted | **yes** |
| `access.X` not a mapping | 277 | identity skipped → over-deny | no |
| `read`/`write` empty / not list | 284–289 | `scopeMatches([],…)`=false → over-deny | no |
| scope not a string | 293 | coerced → over-deny | no |
| invalid scope syntax / traversal | 296 | scope kept, rarely matches → over-deny³ | mostly no³ |
| `rate_limits` invalid | 310–323 | irrelevant to write path, grant admitted | **yes** |

¹ A malformed participant entry contributes no name, so any `access` key
referencing it is unmatched and the TS side over-denies — *but Python still
rejects the file*, so this is a (safe-direction) parity divergence, not a clean
match. ² The file is rejected wholesale, so any *valid-looking* grant in it
(e.g. for a different participant) is a false grant. ³ Over-deny except the
contrived case of a valid grant co-located with an invalid scope in the same
file, where Python rejects the whole file but TS honours the valid grant.

The conclusion: under fail-open, a **complete** false-grant fix would require
mirroring nearly all of acl.py — exactly the TS↔Python drift #160 warns
against. We therefore scope to a **bounded identity layer** (below).

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

## Scope decision: bounded identity layer

We mirror the **participant-identity graph** — the answer to "who is a
well-formed, declared participant that may hold a grant?" — and leave
participant *attributes* and grant *content* to the hub. This is a coherent,
nameable boundary, closes the realistic operator-typo false grants (a grant
keyed on an undeclared or malformed participant), and avoids reimplementing the
attribute/content validation that carries the most acl.py-tracking drift.

The identity layer is exactly the set-shape acl.py enforces at :184–185,
:189–208, and :264–274: `participants` is a non-empty list; every entry is a
well-formed unique named participant; and `set(participant names) ==
set(access keys)` (acl.py requires participants ⊆ access at :268 **and** access
⊆ participants at :273, i.e. set equality).

**Explicitly out (hub-only, documented residual false grants):** participant
`type`/`transport`/`default_scope`/`metadata` (acl.py:211–248), `access.X`
read/write shape and scope syntax (acl.py:284–299), and `rate_limits`
(acl.py:310–323). A `vault.yaml` that is identity-valid but trips one of these
will still produce a TS grant the hub rejects. This residual is acceptable: it
requires hand-editing a spoke's local `vault.yaml` into a state that is valid at
the identity layer but invalid at the attribute/content layer, and the hub
remains the authoritative gate. It is called out in the module doc comment.

## Design

### Section 1 — TS parser change (`mcp-server/src/vault-acl.ts`)

After `loadVaultAcl` reads and YAML-parses `vault.yaml` (existing missing-file /
unparseable / missing-`access` fall-open branches unchanged), add an
identity-layer validation block. On **any** failure below: `console.warn` +
`return null` (fail-open, identical mechanism to the existing branches). Each
check mirrors a specific acl.py line.

1. **`participants` is a non-empty array** (acl.py:184–185). Else → null.
2. **Extract participant names, validating each entry** (acl.py:189–208). For
   each entry: a bare string is its own name; a mapping uses its `name` field.
   An entry that is neither a string nor a mapping with a non-empty **string**
   `name`, or whose name fails `NAME_RE = /^[a-z][a-z0-9-]*$/`, makes the file
   invalid → null. (Mirrors acl.py's per-entry errors at :193, :198, :201.)
3. **No duplicate participant names** (acl.py:206–207). Else → null.
4. **`set(participant names) == set(access keys)`** (acl.py:268 + :273). Else →
   null.

**Gates are short-circuit, evaluated in order 1→4: return `null` on the first
failing gate, do not accumulate.** This matters for gate 4: the participant-name
set it compares against the access keys must contain **only names that passed
gate 2**. Otherwise a bad-regex name (e.g. `Alice`) would land in the set, gate 4
`{Alice} == {Alice}` would pass, and gate 2 would be silently inert for the
`reject-bad-participant-name` fixture. (acl.py *accumulates* and keeps the bad
name in `participant_names` at :264, but still raises because the regex error is
recorded; the TS mirror reaches the same reject verdict only by short-circuiting
at gate 2.)

`NAME_RE` is defined once as a module constant mirroring acl.py's `NAME_RE`
(`/^[a-z][a-z0-9-]*$/`, acl.py:17). No change to `canWrite`, `scopeMatches`, or
`deriveScope`. No caller change in `tools.ts` (`null` already means "skip"). The
module doc comment is updated to list the identity-layer fail-open cases and to
name the hub-only residual.

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

New reject fixtures — one per identity gate, each constructed to trip **only**
its target gate (so Python rejects for the one stated reason and the test
isolates the gate):

1. `reject-missing-participants` — valid `access`, no `participants:` key
   (gate 1 / acl.py:184).
2. `reject-bad-participant-name` — `participants: [{name: Alice}]` (capital,
   fails `NAME_RE`), `access: {Alice: …}` (gate 2 / acl.py:201).
3. `reject-duplicate-participant` — `participants: [{name: alice},{name: alice}]`,
   `access: {alice: …}` (gate 3 / acl.py:207).
4. `reject-access-not-participant` — `participants: [{name: alice}]`,
   `access: {alice: …, bob: …}` (access ⊄ participants; gate 4 / acl.py:273).
   Includes `alice` so it does **not** also trip the participant-no-access path.
5. `reject-participant-no-access` — `participants: [{name: alice},{name: bob}]`,
   `access: {alice: …}` (participant ⊄ access; gate 4 / acl.py:268).

Every reject fixture's *valid-looking* `access` entries use non-empty
`read`/`write` lists with valid scopes, so Python rejects for exactly the
identity reason (no spurious attribute/content errors muddying the assertion).

Harness edits — both branch on the case-file shape (`Array.isArray` /
`isinstance(..., list)`):

- `cli/tests/test_acl_parity.py` — load `cases.json`; if it is a dict with
  `reject: true`, assert `with pytest.raises(ACLError): parse_vault_yaml(path)`;
  else run the existing per-case `can_write` loop.
- `mcp-server/tests/vault-acl.test.ts` — in the parity loop, if the parsed cases
  object has `reject === true`, write the fixture to a temp vault and
  `expect(loadVaultAcl(dir)).toBeNull()`; else run the existing per-case
  `canWrite` loop. **The unconditional `expect(acl).not.toBeNull()` currently at
  the top of the loop body (vault-acl.test.ts:180) moves inside the accept
  branch** — otherwise it fires for reject fixtures.
- `cli/schist/acl-fixtures/README.md` — document both `.cases.json` shapes.

The `expect(yamlFiles.length).toBeGreaterThanOrEqual(4)` sanity check in the TS
harness still holds (we add fixtures, never remove).

## Existing-test regression to fix

The `loadVaultAcl` unit test "coerces non-string write entries"
(vault-acl.test.ts:111–122) uses a `vault.yaml` with **no `participants` key**.
After gate 1, `loadVaultAcl` returns `null` and the test's
`expect(acl).not.toBeNull()` breaks. Fix: add a matching `participants` block
(and ensure `set(participants) == set(access keys)`) to that test's fixture so
it still exercises write-entry coercion through a now-valid identity layer. Audit
the other `loadVaultAcl` unit tests in the same file for the same dependency
("parses a valid vault.yaml" already has `participants`; the null-returning ones
are unaffected).

## Testing

TDD order — **the harness branching and the reject fixtures must land in the
same step** (adding fixtures without the branching makes the TS harness iterate
the reject object as a `ParityCase[]`, producing garbled failures, not a clean
RED):

1. **Step A (harness + fixtures together).** Add the case-shape branching to
   both harnesses and add the five reject fixtures. The Python branch goes green
   immediately (`parse_vault_yaml` already raises for all five today). The TS
   branch goes **clean RED**: the gates don't exist yet, so `loadVaultAcl`
   returns a non-null ACL and the reject branch's `toBeNull()` fails.
2. **Step B (gates).** Add the four TS identity gates; the TS reject-fixture
   assertions flip to green.
3. **Step C (regression).** Fix the "coerces non-string write entries" fixture
   (above).

Full verification:

- `cd mcp-server && npm test` — full Jest suite (parity + unit gates + coercion).
- `python -m pytest cli/tests/test_acl_parity.py` — Python parity branch.
- `python -m pytest cli/tests/` — confirm no acl.py regressions.

## Out of scope

- Mirroring the participant-**attribute** and grant-**content** validation
  (`type`, `transport`, `default_scope`, `metadata`, `read`/`write` shape, scope
  syntax, `rate_limits`). These remain hub-only; the residual false grants they
  leave are documented above and accepted (require an identity-valid but
  attribute/content-invalid hand-edit; hub stays authoritative). Mirroring them
  is the high-drift surface #160 warns against. YAGNI.
- Any change to fail-open vs fail-closed posture. Settled above.
- Caller/`tools.ts` changes. `null` already means skip.
