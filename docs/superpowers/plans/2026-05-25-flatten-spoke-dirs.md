# Flatten Spoke Dirs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Flip schist's defaults from per-spoke subdirectory partitioning to flat content-axis directories, and canonicalize `schema/default.yaml` as the single source of truth for the content-axis directory list.

**Architecture:** `schema/default.yaml` becomes canonical. `cli/schist/rate_limit.py` derives `_DEFAULT_NOTE_DIRS` from it at module import (fail-closed on missing/malformed). `mcp-server/src/tools.ts` loads it at MCP startup as fallback for `<vault>/schist.yaml` (fail-open with baked-in mirror + drift test). CLI seed templates flip from `subdirectory` to `flat`; `default_scope: research/<p>` → `global`; participant write lists broaden to content-axis dirs. `subdirectory` mode stays fully supported, no deprecation.

**Tech Stack:** Python 3.12 (cli + tests via pytest), TypeScript (mcp-server, jest ESM), YAML, git. Branch: `feat/flatten-spoke-dirs` (already created; spec already committed at `c32dec1`, correction at `154c005`).

**Reference spec:** `docs/superpowers/specs/2026-05-24-flatten-spoke-dirs-design.md`

> **Implementation deviation (post-Task-1):** Setuptools 82+ rejected the planned `""` package-key for path-traversal package-data. The canonical YAML moved from `schema/default.yaml` to `cli/schist/default.yaml` (inside the Python package). All references in Tasks 2 / 5 / 6 below should use:
> - **Python:** `Path(__file__).resolve().parent / "default.yaml"` (file is sibling of `rate_limit.py` in the package)
> - **TypeScript:** `path.resolve(__dirname, "..", "..", "..", "cli", "schist", "default.yaml")` (from `mcp-server/dist/` go up 3 → `<repo>/cli/schist/default.yaml`)
>
> Task 1 also already updated `cli/schist/commands.py:198` and `schema/SCHEMA.md:199` to point at the new canonical. Task 6's SCHEMA.md edits skip the :199 line and do only the directory-tree expansion + the `.schist/config.yaml` → `<vault>/schist.yaml` correction.

---

## Task 1: Expand canonical `schema/default.yaml` + wire package-data

**Files:**
- Modify: `schema/default.yaml`
- Modify: `cli/pyproject.toml:50-51` (package-data block)

**Goal:** Add all current content-axis dirs to the canonical YAML, and make it ship inside the wheel so `cli/schist/rate_limit.py` can find it post-install.

- [ ] **Step 1: Expand `schema/default.yaml`**

Replace the file contents with:

```yaml
connection_types: [extends, contradicts, supports, replicates, applies-method-of, reinterprets, related]
statuses: [draft, review, final, archived]
directories:
  notes: notes/
  papers: papers/
  concepts: concepts/
  research: research/
  decisions: decisions/
  ops: ops/
  projects: projects/
  logs: logs/
write_branch: drafts
```

(Old file had only `notes` and `concepts`. The other 6 dirs match the current vault layout.)

- [ ] **Step 2: Update `cli/pyproject.toml` package-data**

Replace lines 50-51:

```toml
[tool.setuptools.package-data]
schist = ["*.sql"]
```

with:

```toml
[tool.setuptools.package-data]
schist = ["*.sql"]
# default.yaml lives at repo-level schema/; copied into the wheel via
# the data-files manifest below so rate_limit._load_default_dirs() can
# find it from inside the installed package.
"" = ["schema/default.yaml"]
```

Note: setuptools accepts `""` as the "root" package key for arbitrary path globs. If setuptools rejects this on `uv pip install`, fall back to copying `schema/default.yaml` into `cli/schist/default.yaml` as part of the build (add a `[tool.setuptools.cmdclass]` build hook or a pre-build script). Pick at impl based on first install attempt.

- [ ] **Step 3: Reinstall and verify wheel ships the file**

```bash
cd /orcd/home/002/yibei/schist
uv pip install --system --force-reinstall --no-deps -e ./cli
python -c "from pathlib import Path; import schist; canonical = Path(schist.__file__).resolve().parent.parent.parent / 'schema' / 'default.yaml'; assert canonical.is_file(), canonical; import yaml; data = yaml.safe_load(canonical.read_text()); print(data['directories'])"
```

Expected output (one line, ordered dict-ish):
```
{'notes': 'notes/', 'papers': 'papers/', 'concepts': 'concepts/', 'research': 'research/', 'decisions': 'decisions/', 'ops': 'ops/', 'projects': 'projects/', 'logs': 'logs/'}
```

If FAIL (file not found), use the fallback approach (copy default.yaml into `cli/schist/`).

- [ ] **Step 4: Commit**

```bash
git add schema/default.yaml cli/pyproject.toml
git commit -m "feat(schema): expand default.yaml to canonical content-axis dir list

Adds research/, decisions/, ops/, projects/, papers/, logs/ alongside
the existing notes/ and concepts/. Ships default.yaml inside the wheel
via package-data so installed packages can locate it.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Refactor `rate_limit.py` to derive `NOTE_DIRS` from canonical

**Files:**
- Modify: `cli/schist/rate_limit.py:36-67` (NOTE_DIRS + `_count_note_files`)
- Modify: `cli/tests/test_rate_limit.py` (add drift test + expanded-coverage tests)

**Goal:** Replace the hardcoded `NOTE_DIRS` tuple with a derived `_DEFAULT_NOTE_DIRS` loaded from `schema/default.yaml` at import. Fails closed if the file is missing/malformed.

- [ ] **Step 1: Write the failing drift test**

Append to `cli/tests/test_rate_limit.py` (after `class TestCountNoteFiles`):

```python
# ---------------------------------------------------------------------------
# Canonical default.yaml drift — _DEFAULT_NOTE_DIRS must match the YAML
# ---------------------------------------------------------------------------


class TestDefaultNoteDirsDrift:
    def test_default_note_dirs_match_canonical_yaml(self):
        """rate_limit._DEFAULT_NOTE_DIRS must mirror schema/default.yaml's
        `directories:` values verbatim. If a contributor adds a new
        content-axis dir to default.yaml, this test fails until they update
        the canonical loader (or stops failing once they do)."""
        import yaml
        from pathlib import Path

        from schist.rate_limit import _DEFAULT_NOTE_DIRS

        canonical_path = (
            Path(__file__).resolve().parent.parent.parent / "schema" / "default.yaml"
        )
        canonical = yaml.safe_load(canonical_path.read_text())
        expected = tuple(canonical["directories"].values())

        assert _DEFAULT_NOTE_DIRS == expected, (
            f"_DEFAULT_NOTE_DIRS drift: expected {expected!r}, "
            f"got {_DEFAULT_NOTE_DIRS!r}. "
            f"Source of truth is {canonical_path}."
        )
```

- [ ] **Step 2: Write the failing expanded-coverage tests**

Add three new test methods inside the existing `class TestCountNoteFiles` in `cli/tests/test_rate_limit.py`:

```python
    def test_subdirectory_counts_research(self):
        files = ["research/2026-05-25-foo.md", "research/sub/bar.md"]
        assert _count_note_files(files, "subdirectory") == 2

    def test_subdirectory_counts_decisions_ops_projects(self):
        files = [
            "decisions/2026-05-25-adr.md",
            "ops/2026-05-25-runbook.md",
            "projects/2026-05-25-kickoff.md",
        ]
        assert _count_note_files(files, "subdirectory") == 3

    def test_subdirectory_counts_logs(self):
        assert _count_note_files(["logs/2026-05-25-session.md"], "subdirectory") == 1
```

- [ ] **Step 3: Run the new tests, verify they fail**

```bash
cd /orcd/home/002/yibei/schist/cli
uv run --with pytest --with . python -m pytest tests/test_rate_limit.py::TestDefaultNoteDirsDrift tests/test_rate_limit.py::TestCountNoteFiles::test_subdirectory_counts_research tests/test_rate_limit.py::TestCountNoteFiles::test_subdirectory_counts_decisions_ops_projects tests/test_rate_limit.py::TestCountNoteFiles::test_subdirectory_counts_logs -v
```

Expected:
- `test_default_note_dirs_match_canonical_yaml` → FAIL with ImportError (`_DEFAULT_NOTE_DIRS` not in module)
- `test_subdirectory_counts_research` / `_decisions_ops_projects` / `_logs` → FAIL (current `NOTE_DIRS` doesn't include research/decisions/ops/projects/logs, so all return 0)

- [ ] **Step 4: Implement the canonical loader**

Replace `cli/schist/rate_limit.py:36-38` (the `NOTE_DIRS = (...)` block including its preceding comment) with:

```python
# Path prefixes considered "note-bearing" for the subdirectory convention.
# Derived at import time from the canonical `schema/default.yaml` so all
# consumers (rate_limit, mcp-server, docs) see the same list. Fails closed
# on missing/malformed file — a broken install must not silently under-count.
def _load_default_dirs() -> tuple[str, ...]:
    import yaml

    # rate_limit.py lives at <repo>/cli/schist/rate_limit.py; default.yaml
    # lives at <repo>/schema/default.yaml. Resolve via parents to keep the
    # path valid for both editable installs and the package-data wheel.
    schema_path = Path(__file__).resolve().parent.parent.parent / "schema" / "default.yaml"
    try:
        data = yaml.safe_load(schema_path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise RuntimeError(
            f"schist install is broken: cannot read canonical {schema_path} ({e}). "
            f"Reinstall the schist package."
        ) from e
    dirs = data.get("directories")
    if not isinstance(dirs, dict) or not dirs:
        raise RuntimeError(
            f"schist install is broken: {schema_path} has no `directories:` mapping."
        )
    return tuple(dirs.values())


_DEFAULT_NOTE_DIRS = _load_default_dirs()
```

Then update `_count_note_files` (currently at lines 53-67) — replace its body's `if scope_convention == "subdirectory":` branch to use `_DEFAULT_NOTE_DIRS` instead of the old `NOTE_DIRS`:

```python
def _count_note_files(changed_files: list[str], scope_convention: str) -> int:
    """Count note-bearing files in a push.

    For ``subdirectory`` convention, only files under one of the content-axis
    directories enumerated in ``schema/default.yaml`` are counted — this is
    uncheatable because non-note files never contribute to the count. For
    ``flat`` and ``multi-vault``, we fall back to a ``.md`` suffix filter
    because there is no canonical directory structure to key off of.
    """
    if scope_convention == "subdirectory":
        return sum(
            1 for f in changed_files
            if any(f.startswith(prefix) for prefix in _DEFAULT_NOTE_DIRS)
        )
    return sum(1 for f in changed_files if f.endswith(".md"))
```

- [ ] **Step 5: Run the full rate_limit test file**

```bash
cd /orcd/home/002/yibei/schist/cli
uv run --with pytest --with . python -m pytest tests/test_rate_limit.py -v
```

Expected: all green, including the new drift test and the three new content-axis tests.

- [ ] **Step 6: Commit**

```bash
git add cli/schist/rate_limit.py cli/tests/test_rate_limit.py
git commit -m "refactor(rate-limit): derive NOTE_DIRS from canonical schema/default.yaml

Replaces the hardcoded NOTE_DIRS tuple with _DEFAULT_NOTE_DIRS loaded at
module import. Fixes a silent under-count bug: pre-receive rate limiting
under \`scope_convention: subdirectory\` was ignoring files under
research/, decisions/, ops/, projects/, and logs/. Fails closed if the
canonical YAML is missing or malformed — a broken install must not let
unbounded pushes through.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Flip flat defaults in `sync.py` seed templates

**Files:**
- Modify: `cli/schist/sync.py:635-658` (`_build_seed_vault`)
- Modify: `cli/schist/sync.py:798-816` (`_build_standalone_vault`)
- Modify: `cli/schist/sync.py` (callers of `_build_seed_vault` that pass `scope_prefix`)
- Modify: `cli/tests/test_init_standalone.py:198`
- Modify or Create: `cli/tests/test_sync.py` (add `_build_seed_vault` / `_build_standalone_vault` template tests)

**Goal:** New hubs and standalone vaults seed with `scope_convention: flat`. Per-participant `default_scope` becomes `"global"`; `access[p].write` becomes the content-axis list.

- [ ] **Step 1: Write the failing seed-builder tests**

Append a new test class to `cli/tests/test_sync.py` (the file exists per `ls cli/tests/test_sync.py`). If the file lacks any imports for `_build_seed_vault` / `_build_standalone_vault`, add them.

```python
# ---------------------------------------------------------------------------
# Seed-vault template — flat default + content-axis writes
# ---------------------------------------------------------------------------


class TestBuildSeedVault:
    def test_seed_uses_flat_scope_convention(self):
        from schist.sync import _build_seed_vault

        data = _build_seed_vault(
            name="hub-x",
            participants=["alice", "bob"],
        )
        assert data["scope_convention"] == "flat"

    def test_seed_participants_default_scope_global(self):
        from schist.sync import _build_seed_vault

        data = _build_seed_vault(name="hub-x", participants=["alice", "bob"])
        for p in data["participants"]:
            assert p["default_scope"] == "global", p

    def test_seed_write_list_is_content_axis(self):
        from schist.sync import _build_seed_vault

        data = _build_seed_vault(name="hub-x", participants=["alice"])
        expected = ["research", "concepts", "decisions", "notes", "ops", "papers"]
        assert data["access"]["alice"]["write"] == expected

    def test_seed_validates_under_acl_parser(self):
        """Generated seed must round-trip through parse_vault_data without errors."""
        from schist.acl import parse_vault_data
        from schist.sync import _build_seed_vault

        data = _build_seed_vault(name="hub-x", participants=["alice", "bob"])
        acl = parse_vault_data(data)
        assert acl.scope_convention == "flat"
        assert acl.get_participant("alice").default_scope == "global"


class TestBuildStandaloneVault:
    def test_standalone_uses_flat_scope_convention(self):
        from schist.sync import _build_standalone_vault

        data = _build_standalone_vault(name="v", identity="local")
        assert data["scope_convention"] == "flat"

    def test_standalone_validates_under_acl_parser(self):
        from schist.acl import parse_vault_data
        from schist.sync import _build_standalone_vault

        data = _build_standalone_vault(name="v", identity="local")
        acl = parse_vault_data(data)
        assert acl.scope_convention == "flat"
```

- [ ] **Step 2: Update the existing `test_init_standalone.py` assertion**

In `cli/tests/test_init_standalone.py:198`, change:

```python
        assert data["scope_convention"] == "subdirectory"
```

to:

```python
        assert data["scope_convention"] == "flat"
```

Also, immediately after that assertion, add:

```python
        # Refactor invariant: standalone seeds must never emit per-spoke
        # subdirectory `default_scope`. See spec 2026-05-24-flatten-spoke-dirs.
        assert "research/" not in (data["participants"][0].get("default_scope") or "")
```

- [ ] **Step 3: Run the new tests, verify they fail**

```bash
cd /orcd/home/002/yibei/schist/cli
uv run --with pytest --with . python -m pytest tests/test_sync.py::TestBuildSeedVault tests/test_sync.py::TestBuildStandaloneVault tests/test_init_standalone.py -v
```

Expected: TestBuildSeedVault and TestBuildStandaloneVault FAIL; test_init_standalone roundtrip FAILs on the scope_convention assertion.

- [ ] **Step 4: Update `_build_seed_vault`**

Replace `cli/schist/sync.py:635-658` (the entire `_build_seed_vault` function) with:

```python
def _build_seed_vault(name: str, participants: list[str]) -> dict:
    """Construct a minimal valid vault.yaml data dict for the hub seed commit.

    Every participant gets `default_scope: global` and a content-axis write
    list. Authorship is recorded in note frontmatter via `source_agent`, not
    in directory placement — see schema/SCHEMA.md and ADR-002 in the vault.
    Hub operators can broaden specific participants (e.g. a privileged spoke
    that manages `shared/skills/`) by editing vault.yaml after init.
    """
    content_axis_write = ["research", "concepts", "decisions", "notes", "ops", "papers"]

    participant_entries = [
        {"name": p, "type": "spoke", "default_scope": "global"}
        for p in participants
    ]
    access = {p: {"read": ["*"], "write": list(content_axis_write)} for p in participants}

    return {
        "vault_version": 1,
        "name": name,
        "scope_convention": "flat",
        "participants": participant_entries,
        "access": access,
    }
```

- [ ] **Step 5: Find and update all callers of `_build_seed_vault`**

```bash
cd /orcd/home/002/yibei/schist
grep -n "_build_seed_vault" cli/schist/sync.py
```

Expected: one or two production callers and the test class added above. For each production caller, remove any `scope_prefix=...` argument (the parameter no longer exists). If a caller relies on per-participant scope substring (search the function body around the caller), confirm nothing downstream still expects the old shape.

- [ ] **Step 6: Update `_build_standalone_vault`**

Replace `cli/schist/sync.py:798-816` (the entire `_build_standalone_vault` function) with:

```python
def _build_standalone_vault(name: str, identity: str) -> dict:
    """Construct a minimal valid vault.yaml data dict for a standalone vault.

    Single-participant, single-agent, full-vault read+write. Kept separate from
    `_build_seed_vault` because the two diverge on participant type and ACL
    shape — parametrizing would hide the difference behind callbacks.
    """
    return {
        "vault_version": 1,
        "name": name,
        "scope_convention": "flat",
        "participants": [{
            "name": identity,
            "type": "agent",
            "default_scope": "global",
        }],
        "access": {identity: {"read": ["*"], "write": ["*"]}},
    }
```

- [ ] **Step 7: Run the full sync + init_standalone test suite**

```bash
cd /orcd/home/002/yibei/schist/cli
uv run --with pytest --with . python -m pytest tests/test_sync.py tests/test_init_standalone.py -v
```

Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add cli/schist/sync.py cli/tests/test_sync.py cli/tests/test_init_standalone.py
git commit -m "feat(init): seed new vaults with scope_convention=flat by default

_build_seed_vault and _build_standalone_vault now emit:
- scope_convention: flat (was: subdirectory)
- per-participant default_scope: global (was: research/<spoke>)
- access[p].write: content-axis dirs (was: [research/<spoke>])

The scope_prefix parameter is dropped from _build_seed_vault. Existing
hubs are not touched; this only affects vaults created by \`schist init\`
going forward. Backward-compat for vault.yaml files with
scope_convention: subdirectory is preserved.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Flip flat default in `spoke_config.py`

**Files:**
- Modify: `cli/schist/spoke_config.py:18`, `cli/schist/spoke_config.py:37`
- Modify: `cli/tests/test_spoke_config.py:19`
- Modify: `cli/tests/test_spoke_config.py` (add explicit subdirectory roundtrip test)

**Goal:** Newly-saved `.schist/spoke.yaml` files (and old ones missing the field) default to `flat`. Existing on-disk `scope_convention: subdirectory` keeps roundtripping.

- [ ] **Step 1: Update tests first (TDD)**

Edit `cli/tests/test_spoke_config.py:19`:

```python
        assert loaded.scope_convention == "subdirectory"
```

becomes:

```python
        assert loaded.scope_convention == "flat"
```

Then add a new test method to the same class (place after the existing `test_custom_scope_convention` at line 31):

```python
    def test_subdirectory_scope_convention_roundtrip(self, tmp_path):
        """Backward-compat: existing spoke.yaml files using subdirectory
        must keep loading without warning. The default flipped to flat in
        spec 2026-05-24-flatten-spoke-dirs, but subdirectory is still a
        fully-supported value."""
        from schist.spoke_config import SpokeConfig, load_spoke_config, save_spoke_config

        config = SpokeConfig(
            hub="url", identity="id", scope="s", scope_convention="subdirectory"
        )
        vault = str(tmp_path / "vault")
        save_spoke_config(vault, config)
        loaded = load_spoke_config(vault)
        assert loaded.scope_convention == "subdirectory"
```

- [ ] **Step 2: Run the tests, verify they fail**

```bash
cd /orcd/home/002/yibei/schist/cli
uv run --with pytest --with . python -m pytest tests/test_spoke_config.py -v
```

Expected: `test_default_scope_convention` (roundtrip → flat) FAILs because the current code defaults to "subdirectory". The new subdirectory roundtrip test PASSes (no code change required to support it — just confirms the existing path still works).

- [ ] **Step 3: Update `spoke_config.py`**

In `cli/schist/spoke_config.py`, change two lines:

Line 18 — `SpokeConfig` dataclass default:
```python
    scope_convention: str = "subdirectory"
```
becomes:
```python
    scope_convention: str = "flat"
```

Line 37 — `load_spoke_config` fallback:
```python
        scope_convention=data.get("scope_convention", "subdirectory"),
```
becomes:
```python
        scope_convention=data.get("scope_convention", "flat"),
```

- [ ] **Step 4: Verify both tests pass**

```bash
cd /orcd/home/002/yibei/schist/cli
uv run --with pytest --with . python -m pytest tests/test_spoke_config.py -v
```

Expected: all green, including the new `test_subdirectory_scope_convention_roundtrip`.

- [ ] **Step 5: Commit**

```bash
git add cli/schist/spoke_config.py cli/tests/test_spoke_config.py
git commit -m "feat(spoke-config): default scope_convention to flat

SpokeConfig dataclass default and load_spoke_config fallback both flip
from subdirectory to flat. Existing spoke.yaml files with
scope_convention: subdirectory keep roundtripping without warning.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Wire `mcp-server/src/tools.ts` to canonical `schema/default.yaml`

**Files:**
- Modify: `mcp-server/src/tools.ts:76-106` (`loadVaultConfig`)
- Modify: `mcp-server/tests/tools.test.ts` (add drift test + canonical-fallback test)

**Goal:** When `<vault>/schist.yaml` does not declare `directories:`, fall back to the canonical list from `schema/default.yaml` rather than a 3-element baked-in literal. Fails open with a stderr warning + baked-in mirror if default.yaml is unreadable. Drift test keeps the mirror in sync.

- [ ] **Step 1: Write the failing drift test**

Append to `mcp-server/tests/tools.test.ts` (after the existing `describe("loadVaultConfig …` block):

```typescript
// ---------------------------------------------------------------------------
// Canonical default.yaml drift — TS fallback must mirror the YAML
// ---------------------------------------------------------------------------

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { load as yamlLoad } from "js-yaml";

import { DEFAULT_DIRECTORIES_FALLBACK } from "../src/tools.js";

describe("default.yaml drift detection", () => {
  test("DEFAULT_DIRECTORIES_FALLBACK mirrors schema/default.yaml directories", () => {
    const canonicalPath = resolve(__dirname, "..", "..", "schema", "default.yaml");
    const raw = yamlLoad(readFileSync(canonicalPath, "utf-8")) as Record<string, unknown>;
    const dirs = raw.directories as Record<string, string>;
    const expected = Object.values(dirs).map((v) => v.replace(/\/$/, ""));
    expect(DEFAULT_DIRECTORIES_FALLBACK).toEqual(expected);
  });
});
```

(Note: the canonical YAML stores paths with trailing slashes for `startswith` matching in Python; the TS side uses bare names for `topLevel` matching in `tools.ts:587`. The test strips trailing slashes for comparison.)

- [ ] **Step 2: Write the failing canonical-fallback test**

Add inside the existing `describe("loadVaultConfig (js-yaml)" …` block in `mcp-server/tests/tools.test.ts`:

```typescript
  test("falls back to canonical schema/default.yaml when schist.yaml omits directories", async () => {
    // schist.yaml has name but no `directories:` field — config should pick
    // up all eight content-axis dirs from the canonical default.yaml.
    const vault = await mkdtemp(join(tmpdir(), "vault-"));
    await writeFile(
      join(vault, "schist.yaml"),
      "name: novel-vault\n",
      "utf-8",
    );
    const config = await loadVaultConfig(vault);
    expect(config.directories).toEqual([
      "notes", "papers", "concepts",
      "research", "decisions", "ops", "projects", "logs",
    ]);
  });
```

(The existing tests already pull in `mkdtemp`, `join`, `tmpdir`, `writeFile`. If not, reuse the same imports the file already uses at the top.)

- [ ] **Step 3: Run the new TS tests, verify they fail**

```bash
cd /orcd/home/002/yibei/schist/mcp-server
npm test -- --testPathPatterns=tools.test
```

Expected: drift test FAILs with `Cannot find module '../src/tools.js'` (DEFAULT_DIRECTORIES_FALLBACK doesn't exist yet); canonical-fallback test FAILs because current default is the 3-element list.

- [ ] **Step 4: Implement canonical loading in `tools.ts`**

Add near the top of `mcp-server/src/tools.ts` (after the existing imports):

```typescript
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Baked-in mirror of schema/default.yaml's `directories:` values. Kept in
// sync by `default.yaml drift detection` in tests/tools.test.ts. Used when
// default.yaml is unreadable at startup (e.g. broken install) so the MCP
// server stays up — fail-open. Asymmetric with rate_limit.py, which fails
// closed: see spec 2026-05-24-flatten-spoke-dirs.
export const DEFAULT_DIRECTORIES_FALLBACK = [
  "notes", "papers", "concepts",
  "research", "decisions", "ops", "projects", "logs",
] as const;

let _canonicalDirsCache: readonly string[] | null = null;

function loadCanonicalDirectories(): readonly string[] {
  if (_canonicalDirsCache !== null) return _canonicalDirsCache;
  try {
    // tools.ts compiles to mcp-server/dist/tools.js; schema/default.yaml
    // lives at <repo>/schema/default.yaml → two levels up from dist.
    const canonicalPath = resolve(__dirname, "..", "..", "schema", "default.yaml");
    const raw = yamlLoad(readFileSync(canonicalPath, "utf-8")) as Record<string, unknown>;
    const dirs = raw.directories as Record<string, string> | undefined;
    if (dirs && typeof dirs === "object") {
      _canonicalDirsCache = Object.values(dirs).map((v) => v.replace(/\/$/, ""));
      return _canonicalDirsCache;
    }
    console.warn(
      `schist: schema/default.yaml at ${canonicalPath} is missing the ` +
      `'directories:' mapping. Using baked-in fallback.`,
    );
  } catch (e) {
    console.warn(
      `schist: schema/default.yaml unreadable (${(e as Error).message}); ` +
      `using baked-in fallback.`,
    );
  }
  _canonicalDirsCache = [...DEFAULT_DIRECTORIES_FALLBACK];
  return _canonicalDirsCache;
}
```

Then update the existing `loadVaultConfig` (around line 95-105). The current `getStringList("directories", ["notes", "papers", "concepts"])` call should become:

```typescript
    directories: getStringList("directories", [...loadCanonicalDirectories()]),
```

(Spread into a fresh array because `getStringList` expects `string[]`, not `readonly string[]`.)

- [ ] **Step 5: Run the TS tests, verify they pass**

```bash
cd /orcd/home/002/yibei/schist/mcp-server
npm test -- --testPathPatterns=tools.test
```

Expected: all green, including both new tests.

- [ ] **Step 6: Run the full TS test suite to catch regressions**

```bash
cd /orcd/home/002/yibei/schist/mcp-server
npm test
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add mcp-server/src/tools.ts mcp-server/tests/tools.test.ts
git commit -m "feat(mcp-server): derive directories fallback from schema/default.yaml

loadVaultConfig now reads schema/default.yaml at startup and uses its
directories mapping as the default when <vault>/schist.yaml omits the
directories: field. Fails open with a baked-in mirror if the canonical
file is unreadable — a drift test holds the mirror in sync with the YAML.

Picks up research/, decisions/, ops/, projects/, logs/, papers/ as
allowed top-level dirs for create_note out of the box.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Update spec docs — `vault-yaml.md`, `SCHEMA.md`, `CLAUDE.md`

**Files:**
- Modify: `schema/vault-yaml.md`
- Modify: `schema/SCHEMA.md`
- Modify: `CLAUDE.md` (project root)

**Goal:** Specification text matches reality and recommends flat.

- [ ] **Step 1: Edit `schema/vault-yaml.md`**

Line 12 — change `scope_convention` row:

```
| scope_convention | string | yes (v1) | How scope maps to filesystem: `subdirectory` (default), `flat`, `multi-vault` |
```

to:

```
| scope_convention | string | yes (v1) | How scope maps to filesystem: `flat` (default, recommended), `subdirectory`, `multi-vault` |
```

Line 42 — extend `default_scope` row:

```
| default_scope | string | no | Scope resolved for `scope: "inherit"` queries. Defaults to `"global"` |
```

becomes:

```
| default_scope | string | no | Scope resolved for `scope: "inherit"` queries. Defaults to `"global"`. Under `scope_convention: flat`, leave at `"global"` — authorship is recorded via the auto-filled `source_agent` frontmatter field, not via directory placement. |
```

Line 83 in the example block — change:

```yaml
scope_convention: subdirectory
```

to:

```yaml
scope_convention: flat
```

And line 93 — change:

```yaml
    default_scope: project:myapp
```

to:

```yaml
    default_scope: global
```

Line 109 — change:

```
- `scope_convention` tells tools how to derive scope from directory structure.
```

to:

```
- `scope_convention` tells tools how to derive scope from directory structure. `flat` is the default; new schist deployments should prefer it. `subdirectory` and `multi-vault` are fully supported for existing deployments.
```

- [ ] **Step 2: Edit `schema/SCHEMA.md`**

Lines 148-159 — replace the Directory Structure block:

```
## Directory Structure (Vault)

```
vault/
├── notes/          # Timestamped research notes
├── papers/         # Paper summaries and analyses
├── concepts/       # Concept node files (stable slugs)
├── logs/           # Session logs, meeting notes
└── .schist/
    ├── schist.db   # SQLite database (auto-generated, gitignored)
    └── config.yaml # Vault-specific schema overrides
```
```

becomes:

```
## Directory Structure (Vault)

```
vault/
├── notes/          # Timestamped research notes
├── papers/         # Paper summaries and analyses
├── concepts/       # Concept node files (stable slugs)
├── research/       # Project-scoped research notes
├── decisions/      # ADRs / decision records
├── ops/            # Runbooks, ops notes
├── projects/       # Project-kickoff and tracking notes
├── logs/           # Session logs, meeting notes
├── shared/         # Cross-spoke shared content (e.g. shared/skills/)
├── vault.yaml      # ACL + scope config (read by pre-receive hook)
├── schist.yaml     # Schema/dir overrides (read by MCP server, optional)
└── .schist/
    └── schist.db   # SQLite database (auto-generated, gitignored)
```

The canonical default directory list lives at `schema/default.yaml`
(shipped with the schist package). `schist.yaml` at the vault root
overrides it per-vault.
```

Lines 163-167 — replace the schema-config opening line:

```
## Schema Configuration

Vaults can override the default schema via `.schist/config.yaml`:
```

becomes:

```
## Schema Configuration

Vaults can override the default schema by placing a `schist.yaml`
file at the vault root. Both `mcp-server` (`loadVaultConfig` in
`tools.ts`) and the `schist schema` CLI (`commands.py:schema`) read
this file, falling back to `schema/default.yaml` when fields are
absent. `vault.yaml` (also at the vault root) is a separate file for
ACL/scope configuration and is parsed by `cli/schist/acl.py`.
```

Line 168 of the example block — change the comment `# .schist/config.yaml` to `# schist.yaml (at vault root)`. Update the `directories:` block in that example to include all eight axis dirs:

```yaml
directories:
  notes: notes/
  papers: papers/
  concepts: concepts/
  research: research/
  decisions: decisions/
  ops: ops/
  projects: projects/
  logs: logs/
  # Add custom:
  experiments: experiments/
```

Line 199 — change:

```
If no config exists, `schema/default.yaml` from the schist installation is used.
```

to:

```
If no `schist.yaml` exists at the vault root, `schema/default.yaml` from the schist installation is used. This is the canonical source of truth for the directory list — both Python (`rate_limit.py`) and TypeScript (`tools.ts`) load it as their default.
```

- [ ] **Step 3: Edit project-root `CLAUDE.md`**

In the "Hub & spoke (multi-machine)" section (currently around line 60), add a new bullet after the "Auto-pull before `get_context`" line:

```
- **Default scope_convention is `flat`:** authorship of a note is recorded in the auto-filled `source_agent` frontmatter, not in directory placement. Notes live in content-axis directories (`research/`, `decisions/`, `ops/`, `projects/`, `notes/`, `papers/`, `concepts/`, `logs/`) regardless of which spoke wrote them. `scope_convention: subdirectory` and `multi-vault` remain supported for existing deployments.
```

- [ ] **Step 4: Commit**

```bash
git add schema/vault-yaml.md schema/SCHEMA.md CLAUDE.md
git commit -m "docs(schema): document flat as default + reconcile config-file paths

vault-yaml.md, SCHEMA.md, and CLAUDE.md now describe scope_convention:
flat as the default. SCHEMA.md's stale '.schist/config.yaml' references
are corrected to <vault>/schist.yaml, with vault.yaml's separate ACL
role called out. Directory-structure example now lists all eight
content-axis dirs that schema/default.yaml carries.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Rename + rewrite the topology guide

**Files:**
- Rename: `docs/hub-spoke-pi-hpc-mac.md` → `docs/hub-spoke-pi-orcd-dragonfly.md`
- Modify (within the renamed file): substitutions throughout 556 lines
- Modify: `CHANGELOG.md` (the entry that references the old filename)
- Modify: `docs/hub-spoke-setup.md` (the inbound link near the top)

**Goal:** The opinionated topology guide reflects the current spoke names (`pi/orcd/dragonfly`, not `pi/hpc/mac`) and the flat scope_convention. Inbound links to the old filename are updated.

- [ ] **Step 1: `git mv` the file**

```bash
cd /orcd/home/002/yibei/schist
git mv docs/hub-spoke-pi-hpc-mac.md docs/hub-spoke-pi-orcd-dragonfly.md
```

- [ ] **Step 2: Apply substitutions inside the renamed file**

These are NOT blind sed replacements — "HPC" the platform name (`HPC cluster`, `HPC login node`) stays as platform terminology; "hpc" the spoke identity becomes `orcd`. Same for "Mac" the platform vs `mac` the spoke. Apply edits with this discrimination:

- **Title (line 1):** `# Hub & Spoke: Pi + HPC + Mac Topology` → `# Hub & Spoke: Pi + ORCD + Dragonfly Topology`
- **Intro bullets (lines 3-7):** "HPC as a spoke" → "ORCD (HPC) as a spoke" (preserve platform context); "Mac (Apple Silicon) as a spoke" → "Dragonfly (Apple Silicon Mac) as a spoke"
- **ASCII diagram (lines 13-31):** redraw with spoke labels `orcd` and `dragonfly`; scope lines change from `research/mac` / `research/hpc` to `research` (single content-axis dir) for both, plus a note in surrounding prose that under flat convention every spoke writes the content-axis dirs and authorship is in `source_agent`.
- **`--participant` lines (~46-48):** `--participant hpc` → `--participant orcd`; `--participant mac` → `--participant dragonfly`
- **SCHIST_IDENTITY examples (~88):** `e.g. hpc, mac` → `e.g. orcd, dragonfly`
- **Lines ~141-168 (Pi-side spoke init walkthrough for Mac):** `--scope research/mac --identity mac` (both occurrences) → `--scope research --identity dragonfly`; `--identity mac` in MCP env examples → `--identity dragonfly`; `SCHIST_AGENT_ID: "mac"`, `SCHIST_IDENTITY: "mac"` → `dragonfly`
- **Lines ~225-270 (HPC-side spoke init):** `--scope research/hpc --identity hpc` → `--scope research --identity orcd`; `export SCHIST_IDENTITY=hpc` → `=orcd`; package install commands stay platform-agnostic
- **Line ~280 (Singularity image build):** `schist-hpc.sif` → `schist-orcd.sif` (the image name is convention, no breaking change)
- **Lines ~283, 299 (scp paths, ssh hostnames):** spoke identity only — `login-node:/scratch/$USER/` stays as a generic placeholder
- **Lines ~325, 344, 363 (run examples with `--dir research/hpc`):** change to `--dir research` (flat content-axis)
- **Lines ~382-383 (summary scope lines):** "HPC writes to `research/hpc/`" → "ORCD (HPC) writes to `research/`"; "Mac writes to `research/mac/`" → "Dragonfly writes to `research/`"
- **Line ~393 (`--dir research/hpc`):** → `--dir research`
- **Line ~404 (`--dir research/mac`):** → `--dir research`
- **Lines ~407-408 (`schist link --source research/mac/... --target research/hpc/...`):** change paths to flat — `--source research/2026-04-24-analysis.md --target research/2026-04-24-training-42.md`

Sweep with the substitutions; verify by re-running `rg "hpc|mac" docs/hub-spoke-pi-orcd-dragonfly.md` and inspecting every remaining hit by hand. Platform-context occurrences ("HPC cluster", "Apple Silicon Mac", "macOS") stay; spoke-identity occurrences flip.

- [ ] **Step 3: Update `CHANGELOG.md`**

Find the entry referencing `docs/hub-spoke-pi-hpc-mac.md` (per `rg "hub-spoke-pi-hpc-mac" CHANGELOG.md`). Update both the filename and the description:

```
- `docs/hub-spoke-pi-hpc-mac.md` — opinionated topology guide for Pi hub + HPC/Mac spoke setup
```

becomes:

```
- `docs/hub-spoke-pi-orcd-dragonfly.md` — opinionated topology guide for Pi hub + orcd (HPC) + dragonfly (Mac) spoke setup
```

- [ ] **Step 4: Update `docs/hub-spoke-setup.md` cross-reference**

The current line (per `rg "hub-spoke-pi-hpc-mac" docs/`):

```
> **Setting up Pi + HPC + Mac?** See the [Pi/HPC/Mac topology guide](hub-spoke-pi-hpc-mac.md)
```

becomes:

```
> **Setting up Pi + ORCD + Dragonfly?** See the [Pi/ORCD/Dragonfly topology guide](hub-spoke-pi-orcd-dragonfly.md)
```

Also: lines 109-110 and 184 of `hub-spoke-setup.md` mention `--scope research/hpc-cluster` and `--scope research/pi`. Update these per the same flat convention:

```
Repeat on the HPC cluster with `--identity hpc-cluster --scope research/hpc-cluster`,
on the Pi with `--identity pi --scope research/pi`, and so on.
```

becomes:

```
Repeat on each spoke with `--identity <spoke-name> --scope research` (or the
content-axis dir the spoke should sparse-checkout). Authorship is recorded in
the auto-filled `source_agent` frontmatter, not via directory placement.
```

And the line 184 example narrating an HPC push:

```
path `research/hpc-cluster/2026-04-12-training-run.md`. The spoke pushes.
```

becomes:

```
path `research/2026-04-12-training-run.md` (with `source_agent: hpc-cluster` in
frontmatter recording authorship). The spoke pushes.
```

- [ ] **Step 5: Verify no stale references remain**

```bash
cd /orcd/home/002/yibei/schist
rg "hub-spoke-pi-hpc-mac" .
rg "research/(hpc|mac|pi|dragonfly|orcd)/" docs/
```

Expected: both `rg` commands return empty (no remaining stale paths).

- [ ] **Step 6: Commit**

```bash
git add docs/hub-spoke-pi-orcd-dragonfly.md docs/hub-spoke-setup.md CHANGELOG.md
git commit -m "docs(hub-spoke): rename topology guide pi-hpc-mac -> pi-orcd-dragonfly

Renames the opinionated three-node topology guide to match the current
spoke identities (hpc -> orcd, mac -> dragonfly) and updates all scope
examples to use flat content-axis directories. Authorship is recorded
in source_agent frontmatter, not in per-spoke subdirectories.

Inbound links updated in CHANGELOG.md and docs/hub-spoke-setup.md
(including the older --scope research/<spoke> examples there).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Cleanup superseded artifacts

**Files:**
- Delete: `docs/refactor-flatten-spoke-dirs.md`
- Delete: `cli/uv.lock.local-pre-pull`

**Goal:** Remove the working-doc handoff (now superseded by the spec) and an obsolete uv.lock backup.

- [ ] **Step 1: Delete the working-doc handoff**

```bash
cd /orcd/home/002/yibei/schist
rm docs/refactor-flatten-spoke-dirs.md
```

(The durable record is ADR-002 at `~/schist-vault/decisions/2026-05-02-adr-002-vault-flat-scopeconvention-with-content-axis-directories.md`; the spec at `docs/superpowers/specs/2026-05-24-flatten-spoke-dirs-design.md` captures the implementation design. This handoff doc has no remaining purpose.)

- [ ] **Step 2: Delete the obsolete uv.lock backup**

```bash
rm cli/uv.lock.local-pre-pull
```

(Per-session backup created 2026-05-10; superseded long ago by the canonical `cli/uv.lock`.)

- [ ] **Step 3: Verify working tree is clean except for the deletions**

```bash
git status
```

Expected: working tree shows the two deletions plus no untracked files (the refactor-flatten-spoke-dirs.md was untracked; uv.lock.local-pre-pull was untracked — both disappear silently from `git status` after `rm`).

- [ ] **Step 4: Commit (combine into the prior commit or stand alone)**

If both files were untracked, neither was in git's index and there's nothing to commit. The deletes are filesystem-only. Run:

```bash
git status
ls docs/refactor-flatten-spoke-dirs.md cli/uv.lock.local-pre-pull 2>&1
```

Expected: status clean, ls shows both files as not-found.

If the files were ever tracked (they shouldn't be), `git add -A` and commit with:

```bash
git commit -m "chore: delete superseded refactor handoff + obsolete uv.lock backup"
```

---

## Task 9: Final integration verification

**Files:** none (verification only).

**Goal:** Confirm the full refactor builds, tests, and roundtrips against the live vault.

- [ ] **Step 1: Full Python test suite**

```bash
cd /orcd/home/002/yibei/schist/cli
uv run --with pytest --with . python -m pytest tests/ -v
```

Expected: every test green, including all the new drift/coverage/template tests.

- [ ] **Step 2: Full TypeScript test suite**

```bash
cd /orcd/home/002/yibei/schist/mcp-server
npm test
```

Expected: every test green, including the TS drift test and canonical-fallback test.

- [ ] **Step 3: Rebuild MCP server**

```bash
cd /orcd/home/002/yibei/schist/mcp-server
npm_config_python=/usr/bin/python3.12 npm run build
```

Expected: clean tsc compile, no errors.

- [ ] **Step 4: `schist doctor` against the live vault**

```bash
schist --vault ~/schist-vault doctor
```

Expected: `MCP schema alignment: in sync (10 required docs columns)` and no new warnings introduced by the refactor.

- [ ] **Step 5: Smoke test — fresh `schist init --hub`**

```bash
TMP=$(mktemp -d)
schist init --hub --hub-path "$TMP/hub" --name smokehub --participant alpha --participant beta
echo "--- seeded vault.yaml ---"
cat "$TMP/hub/vault.yaml"
grep -E "scope_convention:|default_scope:" "$TMP/hub/vault.yaml"
rm -rf "$TMP"
```

Expected output includes:
```
scope_convention: flat
- name: alpha
  type: spoke
  default_scope: global
- name: beta
  type: spoke
  default_scope: global
```

Both participants `access.write` should be the content-axis list.

- [ ] **Step 6: Smoke test — fresh `schist init` standalone**

```bash
TMP=$(mktemp -d)
schist init --path "$TMP/standalone" --name smoke --identity local
grep -E "scope_convention:" "$TMP/standalone/vault.yaml"
rm -rf "$TMP"
```

Expected: `scope_convention: flat`.

- [ ] **Step 7: Verify branch state ready for PR**

```bash
cd /orcd/home/002/yibei/schist
git log --oneline main..HEAD
git status
```

Expected: 7-8 commits (spec + 6-7 implementation commits), clean working tree.

---

## Acceptance recap

When all tasks complete, the spec's acceptance criteria are met:

- ✅ `schema/default.yaml` has all 8 content-axis directories.
- ✅ `cli/schist/rate_limit.py` loads NOTE_DIRS from `default.yaml` at import; the module-level binding `_DEFAULT_NOTE_DIRS` is derived from the YAML.
- ✅ `mcp-server/src/tools.ts` loads the canonical list at startup; hardcoded fallback kept in sync via drift test.
- ✅ `schist init --hub` and `schist init --standalone` generate vault.yaml with `scope_convention: flat` and `default_scope: global`.
- ✅ `SpokeConfig` defaults to `scope_convention: "flat"`.
- ✅ `scope_convention: "subdirectory"` still parses cleanly with no warnings; subdirectory note counting now sees research/decisions/ops/projects/logs.
- ✅ All schist docs reference the flat convention; `hub-spoke-pi-hpc-mac.md` is renamed and rewritten for pi/orcd/dragonfly.
- ✅ `docs/refactor-flatten-spoke-dirs.md` and `cli/uv.lock.local-pre-pull` deleted.
- ✅ `python -m pytest cli/tests/` and `npm test` both green.
- ✅ `schist --vault ~/schist-vault doctor` reports MCP schema alignment in sync.
