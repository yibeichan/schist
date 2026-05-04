# Spoke-setup robustness — design spec

**Date:** 2026-05-03
**Issues:** #41, #40, #43
**PR shape:** 2 PRs (option C from triage):
  - PR A: #41 (init_spoke staging-dir refactor — sync.py)
  - PR B: #40 + #43 (doctor.py additions, no behavior change to init flow)

## Context

All three issues surfaced from the adversarial review of PR #39 (the
SessionEnd hook + MCP doctor work). They are pre-existing footguns —
not regressions introduced by #39 — and they share the theme that
spoke-setup state is currently easy to leave in a broken-but-silent
configuration.

Engineering posture: prefer the structurally durable fix over a
band-aid (e.g. staging+atomic-rename rather than try/except cleanup;
include the "optional" #43 sub-checks because each catches a real
footgun).

## PR A — Issue #41: init_spoke staging refactor

### Problem (recap)

`init_spoke` runs six sequential steps (clone, sparse-checkout, write
spoke.yaml, write `.git/info/exclude`, install hooks, rebuild SQLite).
Failure at any step ≥ 2 leaves the destination dir half-populated.
The user's only recovery is `rm -rf` because re-running fails the
"directory already exists and is not empty" guard.

### Fix

Mirror the standalone-init pattern at `sync.py:588-616`:

1. Reject non-empty destination up front (existing behavior, kept).
2. Compute a sibling staging path:
   `staging = target.parent / f".{target.name}.init-{os.getpid()}"`.
   Same filesystem as `target` ⇒ `os.rename` is atomic.
3. Run all six init steps inside `staging`.
4. On any failure: print error, `shutil.rmtree(staging)` (with safe
   handling of cleanup failure — print "Manual fix: rm -rf <path>"
   and continue), `sys.exit(1)`. Target dir was never created.
5. On success: `os.rename(staging, target)`.

### Refactor shape

Extract the six steps into a helper
`_build_spoke_in_staging(staging, hub, scope, identity, db_path)`
that raises `_InitError` on any step failure (parallel to the
existing `_build_standalone_in_staging`). Keep the user-visible
output (`Cloning from ...`, `Setting up sparse checkout ...`)
inside the helper — the staging path is an implementation detail.

`init_spoke` becomes the thin orchestrator: validate args → make
staging path → call helper in try/except → atomic rename → print
summary. This brings init_spoke to feature parity with the
standalone init's failure-mode handling.

### Tests

In `cli/tests/test_sync.py` (where existing init_spoke tests live):

1. **Happy path regression** — existing test that the fully-built
   spoke has `.git/`, `.schist/spoke.yaml`, hooks, etc. Should
   continue to pass unchanged (no observable behavior change for
   success path).
2. **NEW: failure-mid-init leaves no staging dir.** Inject a
   failure (e.g. invalid scope that breaks sparse-checkout, or
   monkey-patch `_install_local_hooks` to raise) and assert:
   - target dir does not exist after the failure
   - target's parent dir contains no `.{name}.init-*` leftovers
   - re-running `init_spoke` succeeds (no "already exists" rejection)
3. **NEW: cleanup-failure surfaces a "Manual fix" hint** but
   still exits 1 (parallel to the standalone test if one exists;
   add it to both if not).

Use a local file-URL hub or a tmp_path bare repo for these tests
(don't hit the network).

## PR B — Issues #40 + #43: doctor.py additions

Both are additive WARN-level checks. Neither changes existing PASS
behavior except by replacing a too-eager PASS with a more-precise
WARN where appropriate.

### #40 — `check_hooks_path(vault_path)`

New `CheckResult`-returning function in `doctor.py`. Logic:

- If `vault_path` is unset → SKIP.
- Run `git -C <vault_path> config --get core.hooksPath`. If the
  command exits 0 with non-empty output → WARN with detail
  "core.hooksPath is set to '<value>'; schist hooks at
  .git/hooks/ are bypassed" and hint "Unset
  (`git config --unset core.hooksPath`) or symlink the schist
  hooks into <value>/."
- If exit code is non-zero (config not set) → PASS with detail
  "uses default .git/hooks/".

Wire into `run_doctor` between `check_post_commit_hook` and
`check_ingest_available` (close to the existing hooks check).

### #43 — `check_mcp_config` enhancements

Existing `check_mcp_config` returns PASS the moment it locates an
`mcpServers.schist` entry in any candidate config file. Replace
that early PASS with a multi-step validation pass:

1. **Required:** resolve `args[0]` (path to `mcp-server/dist/index.js`)
   and check `Path(args[0]).is_file()`. If missing → WARN with
   detail "MCP entry points at <path> which does not exist" and
   hint to re-run `schist init --print-mcp-config`.
2. **Optional 1 (env match):** if `vault_path` is provided, compare
   the entry's `env.SCHIST_VAULT_PATH` with the resolved current
   `vault_path`. Mismatch → WARN with detail "MCP env
   SCHIST_VAULT_PATH=<x> ≠ current vault <y>".
3. **Optional 2 (auto-detect drift):** auto-detect the current
   `mcp-server/dist/index.js` (same logic as `_print_mcp_config`'s
   fallback at sync.py:719-722). If the auto-detect succeeds and
   differs from `args[0]` → WARN with detail "MCP entry's
   dist/index.js path differs from the auto-detected current
   path" and hint to re-run `schist init --print-mcp-config`.

Aggregation: collect all WARN reasons into a list; if any are
present, return a single WARN with the joined detail (e.g.
"<reason 1>; <reason 2>") and hint pointing at re-running
`--print-mcp-config`. Only return PASS when all three checks
clear, and include the resolved `args[0]` in the PASS detail so
the user can see which entry was matched.

### Tests

In `cli/tests/test_doctor.py`:

1. **#40 — `core.hooksPath` set → WARN** (set via
   `git -C <vault> config core.hooksPath /tmp/whatever`).
2. **#40 — `core.hooksPath` unset → PASS** (default).
3. **#43 — args[0] missing → WARN.**
4. **#43 — env SCHIST_VAULT_PATH mismatch → WARN.**
5. **#43 — auto-detected mcp_path differs from args[0] → WARN.**
6. **#43 — all three clear → PASS** (and PASS detail includes
   `args[0]`).
7. **#43 — multiple sub-failures aggregate into one WARN** with
   joined detail.

For #43 tests, write the stub `mcpServers.schist` entry into a
temporary `.claude/settings.json` and pass that path via the
existing `Path.home()` plumbing — there is precedent in the test
file (verify before writing).

## Out of scope

- The "auto-fix" path for #40 (installing schist hooks into
  `core.hooksPath` location) — issue notes this is riskier
  behavior change, deferred.
- Improving the MCP `create_note` error pass-through (mentioned
  in #48 as low-priority follow-up). Separate issue.

## Rollout

- PR A first (sync.py refactor). Land before PR B so doctor's new
  MCP check sees a stable init flow.
- Existing vaults are unaffected by either PR. No migration step.
- The sister #48 fix (PR #49) is independent and does not block
  either of these.
