# Flatten per-spoke subdirectory partitioning — design spec

**Date:** 2026-05-24
**Origin:** `docs/refactor-flatten-spoke-dirs.md` (handoff doc — superseded by this spec; deleted at impl)
**Driving record:** `~/schist-vault/decisions/2026-05-02-adr-002-vault-flat-scopeconvention-with-content-axis-directories.md`
**PR shape:** 1 PR (entire refactor — schema canon, Python loader, defaults flip, TS fallback, docs, drift tests)

## Context

`~/schist-vault` already migrated to `scope_convention: flat` on 2026-05-02 (per ADR-002 in the vault). Per-spoke directories `research/{hpc,pi,mac}/` were flattened to content-axis dirs (`research/`, `decisions/`, `ops/`, `projects/`), and per-participant `default_scope` reverted to `global`. Spoke identities also evolved: `hpc → orcd`, `mac → dragonfly`.

The **schist code in this repo did not follow.** CLI init templates still hardcode `scope_convention: subdirectory`, the spoke-config dataclass still defaults to `"subdirectory"`, docs still walk users through `--scope research/<spoke>`, and the schist-vault's actual layout (research/, decisions/, ops/, projects/) is not recognized by `rate_limit.py:NOTE_DIRS` — so under subdirectory mode it silently under-counts.

Beyond the original "flatten" scope, exploration surfaced **four separate "what is a note-bearing directory" lists** across the codebase, all out of sync with the vault and with each other:

| Source | List | Used by |
|---|---|---|
| `schema/default.yaml` | `{notes, concepts}` | `cli/schist/commands.py:198` |
| `cli/schist/rate_limit.py:38` | `("notes/", "papers/", "concepts/")` | rate-limit note counting |
| `mcp-server/src/tools.ts:98` fallback | `["notes", "papers", "concepts"]` | `create_note` `dir` validation |
| Actual vault layout | notes, papers, concepts, research, decisions, ops, projects, shared, logs | reality |

This refactor adopts the **C** scope: not just flip defaults, but make `schema/default.yaml` the single source of truth and have both consumers derive from it, with drift tests to keep it that way.

## Design decisions (interview record)

1. **`scope_convention: subdirectory`** — keep fully supported, no deprecation warning. Existing/external deployments must continue to parse without noise.
2. **Hub seed write list** — generic content-axis: `[research, concepts, decisions, notes, ops, papers]`. Per-spoke `default_scope: global`. No `*` or `shared/skills` baked into the generic template.
3. **NOTE_DIRS** — expanded to all current content-axis dirs, sourced from canonical `schema/default.yaml`.
4. **SCHEMA.md doc fix** — included in this PR (stale paths contradict the work being done).
5. **`docs/hub-spoke-pi-hpc-mac.md`** — rename to `docs/hub-spoke-pi-orcd-dragonfly.md`, rewrite hpc→orcd / mac→dragonfly, flip scopes to flat.
6. **Approach C** — canonical schema file, both Python and TypeScript load from it, drift tests guard against future skew.
7. **One PR** — Python + TS + schema + docs in a single coherent change. Drift tests pass end-to-end only with the full set.

## Architecture

Single canonical source at `schema/default.yaml`:

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

**Consumers:**

- `cli/schist/rate_limit.py` — replaces the hardcoded `NOTE_DIRS` constant with `_DEFAULT_NOTE_DIRS` loaded once at module import via `Path(__file__).resolve().parent.parent.parent / "schema" / "default.yaml"`. Fails closed at import (RuntimeError) if file is missing or malformed — a broken install must not silently under-count.
- `mcp-server/src/tools.ts` — in `loadVaultConfig`, loads `path.resolve(__dirname, "../../schema/default.yaml")` and uses its `directories` mapping as the fallback when vault.yaml doesn't declare its own. Fails open: logs a stderr warning and falls back to a baked-in list (which a drift test holds in sync with default.yaml).

**Asymmetric failure modes are intentional:**
- Python (`rate_limit.py`) runs inside `pre-receive` — a short-lived per-push process. A crash there fails-closed at the git level, blocking the bad push with a clear error. Operator notices on the next push.
- TypeScript (`tools.ts`) runs in the long-lived MCP server. A crash would brick every agent in the session. The fail-open pattern is already the established response to config issues in this file.

**Drift detection:** one test per side loads `schema/default.yaml` and asserts the consumer's loaded constant (Python) or hardcoded fallback (TypeScript) matches.

## Components

### Layer 1 — Canonical schema (single edit)

- `schema/default.yaml`: expand `directories:` block from 2 → 8 entries.
- `cli/pyproject.toml`: extend `[tool.setuptools.package-data]` so `default.yaml` ships with the wheel. Either (a) `schist = ["*.sql", "../schema/*.yaml"]` (relative reference) or (b) copy `default.yaml` into `cli/schist/` at build time. **Decision: (a) at impl** unless setuptools rejects the relative path, in which case fall back to (b) with a copy step.

### Layer 2 — Flat-default switch

- `cli/schist/sync.py`:
  - `_build_seed_vault` (~line 635): `scope_convention: "subdirectory"` → `"flat"`; per-participant `default_scope` set to `"global"` (not `f"{scope_prefix}/{p}"`); `access[p].write` → `["research", "concepts", "decisions", "notes", "ops", "papers"]`. Drop the `scope_prefix` parameter and prune its callers in the same commit.
  - `_build_standalone_vault` (~line 798): `scope_convention: "subdirectory"` → `"flat"`.
- `cli/schist/spoke_config.py`:
  - `SpokeConfig.scope_convention` dataclass default (line 18): `"subdirectory"` → `"flat"`.
  - `load_spoke_config` fallback (line 37): same.

### Layer 3 — Canonical-list consumers

- `cli/schist/rate_limit.py`:
  - Delete `NOTE_DIRS = (...)` constant.
  - Add a `_load_default_dirs()` helper that opens `schema/default.yaml` (resolved via `Path(__file__).resolve().parent.parent.parent / "schema" / "default.yaml"` for the editable + sdist installs; if it fails, try `importlib.resources` fallback). Returns `tuple(yaml["directories"].values())` — values already include trailing slashes.
  - Bind result at module level: `_DEFAULT_NOTE_DIRS = _load_default_dirs()`.
  - `_count_note_files`: use `_DEFAULT_NOTE_DIRS` in the `subdirectory` branch; flat/multi-vault branches unchanged.
- `mcp-server/src/tools.ts`:
  - In `loadVaultConfig`, before reading vault.yaml, attempt to load `schema/default.yaml` via `path.resolve(__dirname, "../../schema/default.yaml")` + `yamlLoad`. If it fails, log `WARN: schema/default.yaml unreadable (<err>); using baked-in fallback` and fall back to a hardcoded list that matches default.yaml.
  - Pass the loaded `directories` mapping values (or fallback) as the `getStringList("directories", FALLBACK)` default in the existing call.

### Layer 4 — Docs

- `schema/vault-yaml.md` — flip "subdirectory (default)" → "flat (default)"; add a sentence noting `default_scope` should normally be `"global"` under flat; mention `source_agent` frontmatter as the authorship trace. Update the example block at line 80-104.
- `schema/SCHEMA.md` — update lines 163-199. Doc currently references `.schist/config.yaml` as the schema-override path, but code reads `<vault>/vault.yaml` (mcp-server) and `<vault>/schist.yaml` (`schist schema` CLI). Fix to reflect reality. Also update the directory-structure example at lines 148-159 to include `research/`, `decisions/`, `ops/`, `projects/`.
- `docs/hub-spoke-setup.md` — lines 109-110, 184: rewrite `--scope research/hpc-cluster`, `--scope research/pi` examples to flat scopes (e.g., `--scope research` or `--scope global`; pick at impl based on what spoke-init actually accepts).
- `docs/hub-spoke-pi-hpc-mac.md` → `git mv docs/hub-spoke-pi-orcd-dragonfly.md`. Substitutions throughout the 556-line file: `hpc → orcd`, `mac → dragonfly`, `research/hpc → research`, `research/mac → research`, `schist-hpc.sif → schist-orcd.sif`. Redraw the ASCII topology diagram. Two inbound links to fix in the same commit: `CHANGELOG.md` (the entry describing the doc) and `docs/hub-spoke-setup.md` (cross-reference at the top).
- `CLAUDE.md` (project root) — add a brief note under "Architecture" or "Hub & spoke (multi-machine)" that the default `scope_convention` is `flat` and authorship is recorded in `source_agent` frontmatter.

### Layer 5 — Cleanup

- `rm docs/refactor-flatten-spoke-dirs.md` — superseded by this spec; the durable decision record is ADR-002 in the vault.
- `rm cli/uv.lock.local-pre-pull` — May 10 backup, obsolete. (Unrelated to refactor but housekeeping.)

## Data flow

Both sides hit `schema/default.yaml` once at startup.

```
schema/default.yaml (canonical)
  ├─► [Python, module import]
  │   _load_default_dirs() → _DEFAULT_NOTE_DIRS
  │   used by _count_note_files when scope_convention == "subdirectory"
  │
  └─► [TypeScript, MCP startup]
      loadVaultConfig() reads it as fallback for vault.yaml's `directories:`
      drives create_note `dir` validation
```

Runtime read path unchanged. Hub init / spoke init unchanged in flow — only the seed values written to disk change. Vault validation path (`parse_vault_data`) unchanged — `"flat"` was already in `VALID_SCOPE_CONVENTIONS`.

## Error handling

- **Python**, missing/malformed `default.yaml`: RuntimeError at import → pre-receive hook crashes → push blocked with clear error. Fail-closed.
- **TypeScript**, missing/malformed `default.yaml`: stderr WARN + baked-in fallback. MCP keeps serving. Fail-open. Drift test ensures the fallback stays in sync.
- **Old spoke with `scope_convention: subdirectory`** in vault.yaml or spoke.yaml: parses cleanly, behaves as before with the expanded NOTE_DIRS — no migration required.
- **`pre-commit`** hook is unaffected; the doc rename / file deletion are pure git operations.

## Testing

### New tests

- `cli/tests/test_rate_limit.py`:
  - **Drift test:** load `schema/default.yaml` directly, assert `_DEFAULT_NOTE_DIRS == tuple(yaml["directories"].values())`.
  - **Expanded coverage:** `_count_note_files` under `subdirectory` correctly counts files under `research/`, `decisions/`, `ops/`, `projects/`. Today it silently returns 0 for these.
- `mcp-server/tests/tools.test.ts`:
  - **Drift test:** load `schema/default.yaml` via the same fs path the production code uses; parse; assert the hardcoded fallback list (export it from tools.ts for testability) matches `Object.values(yaml.directories)`.
- `cli/tests/test_sync.py` (or wherever seed-builder tests live; if not yet present, add the file):
  - `_build_seed_vault` emits `scope_convention: flat`, each participant has `default_scope: global`, `access[p].write == ["research", "concepts", "decisions", "notes", "ops", "papers"]`.
- `cli/tests/test_spoke_config.py`:
  - Flip line 19 assertion to `"flat"` for the default-roundtrip.
  - **Add** a separate test asserting `scope_convention="subdirectory"` roundtrips correctly (don't lose backward-compat coverage).
- **Install-smoke test:** runs `python -c "from schist.rate_limit import _DEFAULT_NOTE_DIRS"` against the installed package. Catches "default.yaml didn't ship in the wheel" before merge. Integrate into the existing pytest tree as a subprocess test, or add to CI workflow — pick at impl.

### Modified fixtures

- `cli/tests/test_acl.py:18,37,279` — `_v1()` helper passes `scope_convention="subdirectory"` explicitly; **leave as-is** (test-specific defaults, not asserting system default).
- `cli/tests/test_init_standalone.py:198` — flip `assert data["scope_convention"] == "subdirectory"` → `"flat"`. Add an assertion that the generated vault.yaml contains no `default_scope: research/` substring.
- `cli/tests/test_rate_limit.py:30,36` — change helper default from `"subdirectory"` to `"flat"`; explicitly override in subdirectory-path tests to keep that coverage.
- `cli/tests/test_pre_receive.py:29,502` — leave as-is.

### Verification (impl-time runbook)

```bash
cd /orcd/home/002/yibei/schist
cd cli && uv run --with pytest --with . python -m pytest tests/ -v        # all green
cd ../mcp-server && npm test                                              # all green
cd .. && uv pip install --system -e ./cli                                 # editable reinstall (deps unchanged)
cd mcp-server && npm run build                                            # MCP rebuild
schist --vault ~/schist-vault doctor                                      # MCP schema alignment: in sync
# Smoke: fresh init in a temp dir, inspect the seed vault.yaml
schist init --hub --hub-path /tmp/test-hub --name testhub --participant a --participant b
grep -E 'scope_convention|default_scope' /tmp/test-hub/vault.yaml         # expect: flat, global, global
rm -rf /tmp/test-hub
```

## Acceptance criteria

- `schema/default.yaml` has all 8 content-axis directories.
- `cli/schist/rate_limit.py` loads NOTE_DIRS from `default.yaml` at import; the module-level binding `_DEFAULT_NOTE_DIRS` is derived from the YAML, not a hardcoded literal.
- `mcp-server/src/tools.ts` loads the canonical list at startup; hardcoded fallback is kept in sync via a drift test.
- `schist init --hub` and `schist init --standalone` generate vault.yaml with `scope_convention: flat` and `default_scope: global` for every participant.
- `SpokeConfig` defaults to `scope_convention: "flat"`.
- `scope_convention: "subdirectory"` still parses cleanly with no warnings; pre-receive rate-limit counting under subdirectory mode now sees research/decisions/ops/projects.
- All schist docs reference the flat convention; `hub-spoke-pi-hpc-mac.md` is renamed and rewritten for pi/orcd/dragonfly.
- `docs/refactor-flatten-spoke-dirs.md` deleted (superseded by this spec).
- `python -m pytest cli/tests/` and `npm test` both green.
- `schist --vault ~/schist-vault doctor` reports MCP schema alignment in sync.

## Out of scope

- Migrating any other deployment's vault (this PR only changes defaults; existing vaults are not touched).
- Refactoring the broader schema-config loading machinery: `cli/schist/commands.py:192-210` (`schist.yaml` reader) and `mcp-server/src/tools.ts` per-vault loader paths are not converged to a single source — that's a deeper schema-config refactor.
- Adding a `--scope-convention` flag to `schist init`. If users want subdirectory mode on new vaults, hand-edit the seed vault.yaml after init.
- Touching `cli/schist/commands.py:192-210` schema-command behavior beyond what's required for SCHEMA.md doc accuracy.
- Promoting any new skills to `shared/skills/` — that's a Pi-side workflow.
