# MCP ↔ hub ACL participants parity (#160) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the MCP-side `loadVaultAcl` fail-open gap (#160) by mirroring `acl.py`'s participant-identity validation in the TS parser, pinned by shared reject-case parity fixtures.

**Architecture:** `mcp-server/src/vault-acl.ts:loadVaultAcl` gains a four-gate identity-layer validation block that returns `null` (fail-open, caller skips the local ACL check) on any structural problem in the `participants`/`access` identity graph. A new category of parity fixture — `.cases.json` shaped as `{"reject": true, ...}` instead of an array — asserts that for each invalid file, the Python parser raises `ACLError` **and** the TS parser returns `null`. Participant *attributes* and grant *content* stay hub-only (documented residual).

**Tech Stack:** TypeScript (ESM, Jest 30), Python 3.12 (pytest), `js-yaml`, shared JSON fixtures under `cli/schist/acl-fixtures/`.

**Spec:** `docs/superpowers/specs/2026-05-30-mcp-acl-participants-parity-design.md`

---

## File structure

- **Modify** `mcp-server/src/vault-acl.ts` — add `NAME_RE` module constant; add the four identity gates inside `loadVaultAcl` between the existing missing-`access` guard and the `access`-map build; extend the module doc comment.
- **Modify** `mcp-server/tests/vault-acl.test.ts` — branch the parity loop on `.cases.json` shape (array = accept, object = reject); move the unconditional `expect(acl).not.toBeNull()` into the accept branch; fix the "coerces non-string write entries" fixture to include a `participants` block.
- **Modify** `cli/tests/test_acl_parity.py` — import `ACLError`; branch on `.cases.json` shape (dict with `reject` ⇒ assert `parse_vault_yaml` raises).
- **Create** five fixture pairs under `cli/schist/acl-fixtures/` — `reject-missing-participants`, `reject-bad-participant-name`, `reject-duplicate-participant`, `reject-access-not-participant`, `reject-participant-no-access` (each `.yaml` + `.cases.json`).
- **Modify** `cli/schist/acl-fixtures/README.md` — document the two `.cases.json` shapes.

---

## Task 1: Reject fixtures + README + Python harness branch

This task is green on Python the moment it lands: `parse_vault_yaml` already raises `ACLError` for all five invalid files today. Running the Python parity suite here validates that each fixture is correctly constructed (Python rejects it for exactly its intended reason).

**Files:**
- Create: `cli/schist/acl-fixtures/reject-missing-participants.yaml`
- Create: `cli/schist/acl-fixtures/reject-missing-participants.cases.json`
- Create: `cli/schist/acl-fixtures/reject-bad-participant-name.yaml`
- Create: `cli/schist/acl-fixtures/reject-bad-participant-name.cases.json`
- Create: `cli/schist/acl-fixtures/reject-duplicate-participant.yaml`
- Create: `cli/schist/acl-fixtures/reject-duplicate-participant.cases.json`
- Create: `cli/schist/acl-fixtures/reject-access-not-participant.yaml`
- Create: `cli/schist/acl-fixtures/reject-access-not-participant.cases.json`
- Create: `cli/schist/acl-fixtures/reject-participant-no-access.yaml`
- Create: `cli/schist/acl-fixtures/reject-participant-no-access.cases.json`
- Modify: `cli/schist/acl-fixtures/README.md`
- Test: `cli/tests/test_acl_parity.py`

- [ ] **Step 1: Create the five reject `.yaml` fixtures**

`cli/schist/acl-fixtures/reject-missing-participants.yaml`:
```yaml
vault_version: 1
name: test-reject-missing-participants
scope_convention: flat
access:
  alice:
    read: [notes]
    write: [notes]
```

`cli/schist/acl-fixtures/reject-bad-participant-name.yaml`:
```yaml
vault_version: 1
name: test-reject-bad-name
scope_convention: flat
participants:
  - name: Alice
    type: spoke
    default_scope: global
access:
  Alice:
    read: [notes]
    write: [notes]
```

`cli/schist/acl-fixtures/reject-duplicate-participant.yaml`:
```yaml
vault_version: 1
name: test-reject-duplicate
scope_convention: flat
participants:
  - name: alice
    type: spoke
    default_scope: global
  - name: alice
    type: spoke
    default_scope: global
access:
  alice:
    read: [notes]
    write: [notes]
```

`cli/schist/acl-fixtures/reject-access-not-participant.yaml`:
```yaml
vault_version: 1
name: test-reject-access-not-participant
scope_convention: flat
participants:
  - name: alice
    type: spoke
    default_scope: global
access:
  alice:
    read: [notes]
    write: [notes]
  bob:
    read: [notes]
    write: [notes]
```

`cli/schist/acl-fixtures/reject-participant-no-access.yaml`:
```yaml
vault_version: 1
name: test-reject-participant-no-access
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
    read: [notes]
    write: [notes]
```

- [ ] **Step 2: Create the five reject `.cases.json` fixtures**

`cli/schist/acl-fixtures/reject-missing-participants.cases.json`:
```json
{ "reject": true, "reason": "no 'participants' list (acl.py:184); TS gate 1" }
```

`cli/schist/acl-fixtures/reject-bad-participant-name.cases.json`:
```json
{ "reject": true, "reason": "participant name 'Alice' fails NAME_RE (acl.py:201); TS gate 2" }
```

`cli/schist/acl-fixtures/reject-duplicate-participant.cases.json`:
```json
{ "reject": true, "reason": "duplicate participant name 'alice' (acl.py:207); TS gate 3" }
```

`cli/schist/acl-fixtures/reject-access-not-participant.cases.json`:
```json
{ "reject": true, "reason": "access key 'bob' has no participant (acl.py:273); TS gate 4" }
```

`cli/schist/acl-fixtures/reject-participant-no-access.cases.json`:
```json
{ "reject": true, "reason": "participant 'bob' has no access entry (acl.py:268); TS gate 4" }
```

- [ ] **Step 3: Update the fixtures README**

Append to `cli/schist/acl-fixtures/README.md` after the existing description of the pair convention:

```markdown
## Case-file shapes

`<name>.cases.json` has one of two shapes:

- **Accept** — a JSON *array* of `{ identity, scope, canWrite }` tuples. Both
  parsers must accept the `<name>.yaml` (non-null / no raise) and agree on
  `can_write` / `canWrite` for every tuple.
- **Reject** — a JSON *object* `{ "reject": true, "reason": "<why>" }`. The
  strict Python parser (`parse_vault_yaml`) must raise `ACLError`, and the TS
  reader (`loadVaultAcl`) must return `null` (fail-open). Used to pin the
  participant-identity invariants both parsers enforce (#160).
```

- [ ] **Step 4: Branch the Python parity harness on case-file shape**

In `cli/tests/test_acl_parity.py`, change the import line:
```python
from schist.acl import parse_vault_yaml, ACLError
```

Replace the test function body so it branches on the loaded cases shape:
```python
@pytest.mark.parametrize("yaml_path,cases_path", _fixture_pairs())
def test_acl_matches_fixture(yaml_path: Path, cases_path: Path) -> None:
    assert cases_path.exists(), f"Missing cases file: {cases_path.name}"
    cases = json.loads(cases_path.read_text())

    # Reject fixture: the strict parser must raise, mirroring TS returning None.
    if isinstance(cases, dict) and cases.get("reject"):
        with pytest.raises(ACLError):
            parse_vault_yaml(yaml_path)
        return

    acl = parse_vault_yaml(yaml_path)
    for case in cases:
        actual = acl.can_write(case["identity"], case["scope"])
        assert actual == case["canWrite"], (
            f"{yaml_path.name}: identity={case['identity']!r} "
            f"scope={case['scope']!r} expected {case['canWrite']} got {actual}"
        )
```

- [ ] **Step 5: Run the Python parity suite — expect PASS for all fixtures**

Run:
```bash
cd /orcd/home/002/yibei/schist && python -m pytest cli/tests/test_acl_parity.py -v
```
Expected: PASS. The four existing accept fixtures pass as before; the five new reject fixtures pass because `parse_vault_yaml` raises `ACLError` for each. If any reject fixture fails (no `ACLError` raised), the fixture is mis-constructed — fix the `.yaml` before proceeding.

- [ ] **Step 6: Commit**

```bash
cd /orcd/home/002/yibei/schist
git add cli/schist/acl-fixtures/ cli/tests/test_acl_parity.py
git commit -m "test(acl): reject-case parity fixtures + Python harness branch (#160)"
```

---

## Task 2: TS identity gates + harness branch + regression fix

TDD within this task: the TS harness reject branch goes RED (gates absent ⇒ `loadVaultAcl` returns non-null ⇒ `toBeNull()` fails), then the four gates flip it GREEN. The "coerces non-string write entries" unit test breaks the moment gate 1 lands (its fixture has no `participants`), so it is fixed in the same task before committing.

**Files:**
- Modify: `mcp-server/src/vault-acl.ts`
- Test: `mcp-server/tests/vault-acl.test.ts`

- [ ] **Step 1: Branch the TS parity harness on case-file shape (RED step)**

In `mcp-server/tests/vault-acl.test.ts`, replace the body of the `for (const yamlFile of yamlFiles)` parity loop (the `test(...)` currently titled `${base}: TS canWrite matches every case in ${base}.cases.json`) with:
```ts
    test(`${base}: TS parser matches the fixture contract`, async () => {
      // Build a temp vault containing JUST this fixture as vault.yaml.
      const dir = await fs.mkdtemp(`${os.tmpdir()}/schist-parity-${base}-`);
      tmpDirs.push(dir);
      const yamlBody = readFileSyncForFixtures(pathJoin(FIXTURES_DIR_TS, yamlFile), "utf-8");
      await fs.writeFile(pathJoin(dir, "vault.yaml"), yamlBody, "utf-8");

      const cases: ParityCase[] | { reject?: boolean } = JSON.parse(
        readFileSyncForFixtures(pathJoin(FIXTURES_DIR_TS, `${base}.cases.json`), "utf-8"),
      );

      // Reject fixture: TS must fail open (return null), mirroring acl.py raising ACLError.
      if (!Array.isArray(cases)) {
        const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
        expect(loadVaultAcl(dir)).toBeNull();
        warnSpy.mockRestore();
        return;
      }

      const acl = loadVaultAcl(dir);
      expect(acl).not.toBeNull();
      for (const c of cases) {
        expect({ ...c, actual: canWrite(acl!, c.identity, c.scope) }).toEqual({
          ...c,
          actual: c.canWrite,
        });
      }
    });
```

- [ ] **Step 2: Run the TS parity suite — expect RED on reject fixtures**

Run:
```bash
cd /orcd/home/002/yibei/schist/mcp-server && npm test -- --testPathPatterns=vault-acl
```
Expected: the four accept-fixture parity tests PASS; the five reject-fixture parity tests FAIL with `expect(received).toBeNull()` / "Received: {...}" — `loadVaultAcl` still returns a non-null ACL because the gates don't exist yet. This is the intended RED.

- [ ] **Step 3: Add the `NAME_RE` constant**

In `mcp-server/src/vault-acl.ts`, after the `import` lines and before `export interface AccessEntry`, add:
```ts
/**
 * Participant-name syntax, mirrored verbatim from cli/schist/acl.py NAME_RE.
 * A name is a lowercase letter followed by lowercase letters, digits, hyphens.
 */
const NAME_RE = /^[a-z][a-z0-9-]*$/;
```

- [ ] **Step 4: Add the four identity gates inside `loadVaultAcl`**

In `mcp-server/src/vault-acl.ts`, locate the end of the missing-`access` guard (the `if (!raw || typeof raw !== "object" || !("access" in ...)) { ...; return null; }` block) and the line `const access: VaultAcl["access"] = {};` that follows it. Insert the gate block **between** them:
```ts
  // #160: identity-layer parity with cli/schist/acl.py:parse_vault_data.
  // Gates are short-circuit and evaluated in order; return null (fail-open) on
  // the first failure. This mirrors the participant-identity invariants the hub
  // enforces (participants present; names well-formed + unique; participants and
  // access keys are the same set). Participant ATTRIBUTES (type/transport/
  // default_scope/metadata) and grant CONTENT (read/write shape, scope syntax,
  // rate_limits) are NOT validated here — they remain hub-only, and a
  // vault.yaml that is identity-valid but attribute/content-invalid will still
  // produce a local grant the hub rejects (documented residual).
  const rawObj = raw as { participants?: unknown; access: Record<string, unknown> };

  // Gate 1: participants must be a non-empty array (acl.py:184-185).
  const rawParticipants = rawObj.participants;
  if (!Array.isArray(rawParticipants) || rawParticipants.length === 0) {
    console.warn(`schist: vault.yaml at ${aclPath} has no valid 'participants' list; skipping local ACL check.`);
    return null;
  }

  // Gate 2: every entry is a string or {name: <non-empty string matching NAME_RE>}
  // (acl.py:189-208). Gate 3: no duplicate names (acl.py:206-207). Short-circuit
  // here so the participant-name set used by gate 4 contains only valid names.
  const participantNames = new Set<string>();
  for (const p of rawParticipants) {
    const name: unknown = typeof p === "string"
      ? p
      : (p && typeof p === "object" ? (p as { name?: unknown }).name : undefined);
    if (typeof name !== "string" || name.length === 0 || !NAME_RE.test(name)) {
      console.warn(`schist: vault.yaml at ${aclPath} has a malformed participant name; skipping local ACL check.`);
      return null;
    }
    if (participantNames.has(name)) {
      console.warn(`schist: vault.yaml at ${aclPath} has a duplicate participant name '${name}'; skipping local ACL check.`);
      return null;
    }
    participantNames.add(name);
  }

  // Gate 4: set(participant names) == set(access keys) (acl.py:268 + :273).
  const accessKeys = Object.keys(rawObj.access);
  if (accessKeys.length !== participantNames.size || !accessKeys.every((k) => participantNames.has(k))) {
    console.warn(`schist: vault.yaml at ${aclPath} participants and access keys do not match; skipping local ACL check.`);
    return null;
  }
```

- [ ] **Step 5: Update the `loadVaultAcl` doc comment**

In `mcp-server/src/vault-acl.ts`, extend the JSDoc above `loadVaultAcl` to list the new fail-open cases. After the existing item 3 ("vault.yaml is valid YAML but missing the 'access' mapping"), add:
```
 *   4. vault.yaml fails an identity-layer invariant the hub enforces —
 *      'participants' absent/empty, a malformed or duplicate participant
 *      name, or participant-names != access-keys as sets — warn + skip.
 *      Participant attributes and grant content (scope syntax, read/write
 *      shape, rate_limits) are NOT checked here; they remain hub-only, so a
 *      vault.yaml that is identity-valid but attribute/content-invalid can
 *      still yield a local grant the hub rejects (documented residual, #160).
```

- [ ] **Step 6: Run the TS parity suite — expect GREEN on reject fixtures, RED on coerce test**

Run:
```bash
cd /orcd/home/002/yibei/schist/mcp-server && npm test -- --testPathPatterns=vault-acl
```
Expected: all parity tests now PASS (reject fixtures return null via the gates). One unit test — "coerces non-string write entries defensively" — now FAILS with `expect(acl).not.toBeNull()` because its fixture has no `participants` block and trips gate 1. Fixed in the next step.

- [ ] **Step 7: Fix the "coerces non-string write entries" fixture**

In `mcp-server/tests/vault-acl.test.ts`, in the `test("coerces non-string write entries defensively", ...)` body, change the `makeTempVault` argument to include a matching `participants` block:
```ts
    const dir = await makeTempVault(`
participants:
  - name: alice
access:
  alice:
    read: ["*"]
    write: [notes, 42, null]
`);
```
(The `access` key `alice` now matches the single participant `alice`, so gates 1–4 pass and the test exercises write-entry coercion through a valid identity layer. Leave the two assertions unchanged.)

- [ ] **Step 8: Run the full vault-acl test file — expect all GREEN**

Run:
```bash
cd /orcd/home/002/yibei/schist/mcp-server && npm test -- --testPathPatterns=vault-acl
```
Expected: PASS — every `scopeMatches`, `canWrite`, `loadVaultAcl`, `deriveScope`, and parity test (accept + reject) green.

- [ ] **Step 9: Commit**

```bash
cd /orcd/home/002/yibei/schist
git add mcp-server/src/vault-acl.ts mcp-server/tests/vault-acl.test.ts
git commit -m "fix(mcp-server): mirror hub ACL participant-identity invariants (#160)"
```

---

## Task 3: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Full MCP-server Jest suite**

Run:
```bash
cd /orcd/home/002/yibei/schist/mcp-server && npm test
```
Expected: PASS. Confirms no other MCP test depends on the old loose-parse behavior (e.g. `create_note` / `add_connection` ACL tests that build a `vault.yaml`).

- [ ] **Step 2: Full Python CLI suite**

Run:
```bash
cd /orcd/home/002/yibei/schist && python -m pytest cli/tests/
```
Expected: PASS. Confirms the harness change did not regress `acl.py` or any other CLI test.

- [ ] **Step 3: TypeScript build check**

Run:
```bash
cd /orcd/home/002/yibei/schist/mcp-server && npm run build
```
Expected: clean compile (no TS errors from the new `NAME_RE` constant, `rawObj` cast, or gate block).

- [ ] **Step 4: If any suite fails, stop and investigate** — do not paper over. A failure here means a hidden dependency on the loose parser; fix the root cause (likely a test `vault.yaml` missing a `participants` block) before declaring done.
