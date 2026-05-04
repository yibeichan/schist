# Doctor checks for issues #40 and #43 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two additive doctor checks that catch silent-misconfiguration footguns surfaced by PR #39 review.

- **#40 — `check_hooks_path`** — new check that WARNs when `core.hooksPath` is set, because schist's installed hooks at `.git/hooks/` are silently bypassed.
- **#43 — `check_mcp_config` enhancements** — replace the too-eager early PASS with a validation pass: `args[0]` exists, `SCHIST_VAULT_PATH` env matches current vault, auto-detected `mcp_path` matches `args[0]`. Aggregated WARN reasons surface in one CheckResult.

**Architecture:** Both changes are additive to `cli/schist/doctor.py`. No behavior change to init flow, sync flow, or any other code path. Existing tests in `cli/tests/test_doctor.py` will need their `args[0]` mock paths updated to point at real files (since the enhanced check_mcp_config now validates path existence) — that is a *test-mock update*, not a behavior regression.

**Tech Stack:** Python 3.12+, pytest, `subprocess.run` for `git config`, `pathlib.Path` for filesystem checks.

**Design spec:** `docs/superpowers/specs/2026-05-03-spoke-setup-robustness-design.md` (PR B section).

**Builds on:** PR #51 (`refactor/issue-41-spoke-init-staging`). This branch — `feat/issues-40-43-doctor-checks` — is forked off PR #51's tip. Once #51 merges, this branch will need a rebase onto main (GitHub's "Update branch" handles it). Until then, the PR will show "Closes #41 + #40 + #43" in its diff because PR A's commits are inherited.

---

## Task 1: Branch + commit spec fix + plan

The branch is already created (`feat/issues-40-43-doctor-checks`, off PR #51's tip). The spec drift fix from PR #51's review (helper signature dropped `db_path`) is already applied to the spec doc in this branch's working tree, alongside this plan doc. Commit them together.

**Files:**
- Modify (commit): `docs/superpowers/specs/2026-05-03-spoke-setup-robustness-design.md` (spec drift fix in the "Refactor shape" section)
- Create (commit): `docs/superpowers/plans/2026-05-03-doctor-checks.md` (this file)

- [ ] **Step 1: Confirm branch + working tree state**

```bash
git status -sb
```

Expected: branch `feat/issues-40-43-doctor-checks`, two modified/untracked files exactly: the spec edit and the new plan file. Other untracked items (`.gstack/`, `docs/refactor-flatten-spoke-dirs.md`, `.claude/.nfs*`) must NOT be staged.

- [ ] **Step 2: Commit the spec fix + plan**

```bash
git add docs/superpowers/specs/2026-05-03-spoke-setup-robustness-design.md \
        docs/superpowers/plans/2026-05-03-doctor-checks.md
git commit -m "docs: PR B plan + spec signature fix for _build_spoke_in_staging"
```

Expected: a single commit with two doc files.

---

## Task 2: TDD — failing tests for `check_hooks_path` (#40)

Write the test class for the new doctor check before the function exists. The tests will fail with `ImportError: cannot import name 'check_hooks_path' from 'schist.doctor'` until Task 3 implements it.

**Files:**
- Modify: `cli/tests/test_doctor.py` — add a new `TestCheckHooksPath` class after the existing `TestCheckPostCommitHook` (around line 180), and update the import block at the top.

- [ ] **Step 1: Add `check_hooks_path` to the import block**

Find the existing import block at the top of `cli/tests/test_doctor.py` (lines 13-27):

```python
from schist.doctor import (
    CheckResult,
    check_git,
    check_ingest_available,
    check_mcp_config,
    check_node,
    check_post_commit_hook,
    check_python,
    check_schist_yaml,
    check_spoke,
    check_sqlite,
    check_vault_exists,
    check_vault_is_git,
    run_doctor,
)
```

Replace with:

```python
from schist.doctor import (
    CheckResult,
    check_git,
    check_hooks_path,
    check_ingest_available,
    check_mcp_config,
    check_node,
    check_post_commit_hook,
    check_python,
    check_schist_yaml,
    check_spoke,
    check_sqlite,
    check_vault_exists,
    check_vault_is_git,
    run_doctor,
)
```

- [ ] **Step 2: Add the `TestCheckHooksPath` class**

Insert immediately after the existing `TestCheckPostCommitHook` class (find by name; the line number is approximate):

```python
class TestCheckHooksPath:
    """Issue #40 — warn when core.hooksPath redirects git away from
    .git/hooks/ so schist's installed hooks are silently bypassed."""

    def test_no_path(self):
        r = check_hooks_path(None)
        assert r.status == "SKIP"

    def test_unset_returns_pass(self, tmp_path):
        # Init a fresh repo with no core.hooksPath set.
        subprocess.run(["git", "init", str(tmp_path)], check=True,
                       capture_output=True)
        r = check_hooks_path(str(tmp_path))
        assert r.status == "PASS"
        assert r.label == "Hooks path"

    def test_set_returns_warn(self, tmp_path):
        """When core.hooksPath is set to a non-default value, the schist
        hooks at .git/hooks/ are bypassed — warn loudly."""
        subprocess.run(["git", "init", str(tmp_path)], check=True,
                       capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.hooksPath", "/tmp/elsewhere"],
            check=True, capture_output=True,
        )
        r = check_hooks_path(str(tmp_path))
        assert r.status == "WARN"
        assert "core.hooksPath" in r.message
        assert "/tmp/elsewhere" in r.message
        assert r.fix is not None

    def test_not_a_git_repo(self, tmp_path):
        """If the vault path isn't a git repo at all, SKIP (other doctor
        checks will FAIL appropriately for the missing .git/)."""
        r = check_hooks_path(str(tmp_path))
        assert r.status == "SKIP"
```

- [ ] **Step 3: Run the new tests — verify they fail with ImportError**

```bash
uv run pytest cli/tests/test_doctor.py::TestCheckHooksPath -v
```

Expected: FAIL — `ImportError: cannot import name 'check_hooks_path' from 'schist.doctor'`. This is the RED state of TDD; the failing tests document the contract Task 3 will satisfy.

- [ ] **Step 4: Do NOT commit yet**

The failing tests will be committed together with the implementation in Task 3 (single semantic unit: "add check_hooks_path with tests"). Leave the working tree dirty for Task 3.

---

## Task 3: Implement `check_hooks_path` + wire into `run_doctor` (#40)

**Files:**
- Modify: `cli/schist/doctor.py` — add `check_hooks_path` function, wire into `run_doctor` checks list.

- [ ] **Step 1: Add the function**

Insert immediately after `check_post_commit_hook` (around line 156, before `check_ingest_available`):

```python
def check_hooks_path(vault_path: Optional[str]) -> CheckResult:
    """Warn if `core.hooksPath` is set — schist's hooks at .git/hooks/
    are silently bypassed when this config is non-default.

    See issue #40. Common cause: user runs a pre-commit framework or
    shared team hooks under `~/.git-hooks/` and never installed schist's
    post-commit ingester / pre-commit secret guard there.
    """
    if not vault_path:
        return CheckResult("SKIP", "Hooks path", "skipped (no vault)")
    if not (Path(vault_path) / ".git").exists():
        return CheckResult("SKIP", "Hooks path", "skipped (not a git repo)")
    try:
        result = subprocess.run(
            ["git", "-C", vault_path, "config", "--get", "core.hooksPath"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return CheckResult("SKIP", "Hooks path", f"git config check failed: {e}")

    # `git config --get` exits 1 when the key is unset.
    if result.returncode == 0 and result.stdout.strip():
        configured = result.stdout.strip()
        return CheckResult(
            "WARN", "Hooks path",
            f"core.hooksPath is set to '{configured}' — schist hooks at .git/hooks/ are bypassed",
            fix=(
                f"Either unset (`git -C {vault_path} config --unset core.hooksPath`) "
                f"or symlink schist's hooks into {configured}/."
            ),
        )
    return CheckResult("PASS", "Hooks path", "uses default .git/hooks/")
```

- [ ] **Step 2: Wire into `run_doctor`**

In `run_doctor` (around line 251), find the `checks = [...]` list. Insert `check_hooks_path(vault_path)` immediately after `check_post_commit_hook(vault_path)`:

```python
    checks = [
        check_python(),
        check_node(),
        check_git(),
        check_vault_exists(vault_path),
        check_vault_is_git(vault_path),
        check_schist_yaml(vault_path),
        check_sqlite(vault_path, db_path),
        check_post_commit_hook(vault_path),
        check_hooks_path(vault_path),
        check_ingest_available(vault_path),
        check_spoke(vault_path),
        check_mcp_config(vault_path),
    ]
```

- [ ] **Step 3: Run Task 2's failing tests — they should now PASS**

```bash
uv run pytest cli/tests/test_doctor.py::TestCheckHooksPath -v
```

Expected: 4 tests PASS.

- [ ] **Step 4: Run the existing `TestRunDoctor` integration test**

`test_full_vault` may need updating — it asserts every vault check is PASS, and now there's a new check (`Hooks path`) in the checks list.

```bash
uv run pytest cli/tests/test_doctor.py::TestRunDoctor::test_full_vault -v
```

If this fails because the new check is missing from `vault_labels`, update that test (line 316-317) to include `"Hooks path"`:

```python
        vault_labels = {"Vault", "Git repo", "schist.yaml", "SQLite",
                        "Post-commit hook", "Hooks path", "Ingest"}
```

Re-run; expected PASS.

- [ ] **Step 5: Do NOT commit yet** — Task 5 commits #40 + #43 implementations together with one message.

---

## Task 4: TDD — failing tests for `check_mcp_config` enhancements (#43)

The existing `check_mcp_config` returns PASS as soon as it locates an `mcpServers.schist` entry. The enhancement adds three sub-checks:

1. **Required:** `Path(args[0]).is_file()`. WARN if missing.
2. **Optional:** `env.SCHIST_VAULT_PATH` matches the resolved current `vault_path`. WARN if mismatch.
3. **Optional:** auto-detected current `mcp_path` (sync.py:719-722 logic) matches `args[0]`. WARN if differs.

Aggregate failures into a single WARN with joined detail. PASS only when all clear; PASS detail includes resolved `args[0]`.

**Files:**
- Modify: `cli/tests/test_doctor.py` — extend the existing `TestCheckMcpConfig` class.

- [ ] **Step 1: Update existing tests to use real `args[0]` paths**

The existing `TestCheckMcpConfig` tests at lines 244-269 use `"args": ["/path/to/index.js"]` which no longer exists on disk. After the enhancement, that path makes the check WARN with "MCP entry points at <path> which does not exist". Existing tests that expect PASS will break.

Update each existing PASS test (`test_found_in_claude_code_user_config`, `test_found_in_claude_desktop_settings`) to:

1. Create a real fake mcp file in `tmp_path` (e.g. `mcp_path = tmp_path / "fake-mcp" / "index.js"; mcp_path.parent.mkdir(parents=True); mcp_path.write_text("// stub\n")`).
2. Use `str(mcp_path)` as `args[0]` in the JSON config.
3. Continue to assert `r.status == "PASS"`.

The `test_not_found` test stays unchanged.

Concrete example for `test_found_in_claude_code_user_config`:

```python
    def test_found_in_claude_code_user_config(self, tmp_path, monkeypatch):
        """Claude Code (the active product) stores user-scope MCP servers in
        ~/.claude.json — distinct from Claude Desktop's ~/.claude/settings.json.
        """
        fake_mcp = tmp_path / "fake-mcp" / "dist" / "index.js"
        fake_mcp.parent.mkdir(parents=True)
        fake_mcp.write_text("// stub\n")
        (tmp_path / ".claude.json").write_text(json.dumps({
            "mcpServers": {"schist": {"command": "node", "args": [str(fake_mcp)]}}
        }))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(None)
        assert r.status == "PASS"
        assert ".claude.json" in r.message
```

Apply the same pattern to `test_found_in_claude_desktop_settings`.

- [ ] **Step 2: Add new tests for the three sub-checks**

Append to `TestCheckMcpConfig`:

```python
    def test_args0_missing_returns_warn(self, tmp_path, monkeypatch):
        """Issue #43 sub-check 1: WARN when args[0] doesn't exist on disk."""
        (tmp_path / ".claude.json").write_text(json.dumps({
            "mcpServers": {"schist": {
                "command": "node",
                "args": [str(tmp_path / "nope" / "missing.js")],
            }}
        }))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(None)
        assert r.status == "WARN"
        assert "does not exist" in r.message

    def test_vault_path_env_mismatch_returns_warn(self, tmp_path, monkeypatch):
        """Issue #43 sub-check 2: WARN when entry's SCHIST_VAULT_PATH env
        differs from the current vault_path passed to the doctor."""
        fake_mcp = tmp_path / "fake-mcp" / "dist" / "index.js"
        fake_mcp.parent.mkdir(parents=True)
        fake_mcp.write_text("// stub\n")
        current_vault = tmp_path / "current-vault"
        current_vault.mkdir()
        wrong_vault = tmp_path / "old-vault"
        wrong_vault.mkdir()
        (tmp_path / ".claude.json").write_text(json.dumps({
            "mcpServers": {"schist": {
                "command": "node",
                "args": [str(fake_mcp)],
                "env": {"SCHIST_VAULT_PATH": str(wrong_vault)},
            }}
        }))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(str(current_vault))
        assert r.status == "WARN"
        assert "SCHIST_VAULT_PATH" in r.message

    def test_aggregates_multiple_warnings(self, tmp_path, monkeypatch):
        """Multiple sub-check failures aggregate into ONE WARN result whose
        message lists each failure (joined with '; ')."""
        # Both args[0] missing AND env mismatch in the same entry.
        current_vault = tmp_path / "current"
        current_vault.mkdir()
        (tmp_path / ".claude.json").write_text(json.dumps({
            "mcpServers": {"schist": {
                "command": "node",
                "args": [str(tmp_path / "nope.js")],
                "env": {"SCHIST_VAULT_PATH": str(tmp_path / "old")},
            }}
        }))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(str(current_vault))
        assert r.status == "WARN"
        assert "does not exist" in r.message
        assert "SCHIST_VAULT_PATH" in r.message
        assert "; " in r.message  # aggregated, not just one reason
```

The auto-detect sub-check (3) is hard to test without monkeypatching the auto-detect logic itself. Add it as a coarser test:

```python
    def test_auto_detect_drift_returns_warn(self, tmp_path, monkeypatch):
        """Issue #43 sub-check 3: WARN when the entry's args[0] differs from
        the auto-detected current mcp-server/dist/index.js."""
        # Set up a real-on-disk args[0] (so sub-check 1 passes)
        entry_mcp = tmp_path / "old-checkout" / "dist" / "index.js"
        entry_mcp.parent.mkdir(parents=True)
        entry_mcp.write_text("// stale\n")

        # Patch the auto-detect helper to return a different path.
        # The enhanced check_mcp_config calls a private helper for this; the
        # test patches that helper to return a synthetic 'current' path.
        # If implementation uses an inline auto-detect, the patch target is
        # `schist.doctor._auto_detect_mcp_path` (extract one in Task 5).
        with patch("schist.doctor._auto_detect_mcp_path",
                   return_value=str(tmp_path / "fresh-checkout" / "dist" / "index.js")):
            (tmp_path / ".claude.json").write_text(json.dumps({
                "mcpServers": {"schist": {
                    "command": "node",
                    "args": [str(entry_mcp)],
                }}
            }))
            monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
            r = check_mcp_config(None)
            assert r.status == "WARN"
            assert "auto-detected" in r.message or "differs" in r.message
```

- [ ] **Step 3: Run new tests — verify they fail**

```bash
uv run pytest cli/tests/test_doctor.py::TestCheckMcpConfig -v
```

Expected: the existing PASS tests now FAIL (because they need the args[0] path update from Step 1 — confirm Step 1 was applied), and the four new tests FAIL because the enhancement isn't implemented yet.

If the existing PASS tests were updated correctly in Step 1 but they STILL fail because today's check_mcp_config returns PASS without checking args[0] — that's actually FINE for those specific tests (today returns PASS regardless, which is what they expect; tomorrow, with real args[0], they'll continue to expect PASS). So if Step 1 + today's logic = PASS, leave as-is. The four new tests are the ones that must fail today.

- [ ] **Step 4: Do NOT commit** — Task 5 commits #40 + #43 implementations together.

---

## Task 5: Implement `check_mcp_config` enhancements (#43)

**Files:**
- Modify: `cli/schist/doctor.py` — extend `check_mcp_config`, add `_auto_detect_mcp_path` helper.

- [ ] **Step 1: Add `_auto_detect_mcp_path` helper**

Insert immediately before `check_mcp_config` (around line 205):

```python
def _auto_detect_mcp_path() -> Optional[str]:
    """Locate `mcp-server/dist/index.js` relative to this checkout.

    Mirrors the fallback logic in sync.py:_print_mcp_config (lines 719-722).
    Returns the absolute path if found; None if not (e.g. distribution-
    installed schist with no source checkout).
    """
    pkg_dir = Path(__file__).resolve().parents[2]
    candidate = pkg_dir / "mcp-server" / "dist" / "index.js"
    if candidate.exists():
        return str(candidate.resolve())
    return None
```

- [ ] **Step 2: Replace `check_mcp_config` body**

Replace the function body (lines 206-248) with the validating version:

```python
def check_mcp_config(vault_path: Optional[str]) -> CheckResult:
    """Check if schist is configured in Claude Code or Cursor settings.

    Beyond locating the entry, validates (issue #43):
      1. args[0] exists on disk (REQUIRED)
      2. env.SCHIST_VAULT_PATH matches vault_path if provided
      3. args[0] matches the auto-detected current mcp-server path
    """
    candidates = [
        # Claude Code (active product) stores user-scope MCP servers here.
        # Same `mcpServers` shape as Claude Desktop, different path.
        Path.home() / ".claude.json",
        # Claude Desktop / settings.json paths (legacy and project-scoped).
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
    ]
    if vault_path:
        candidates.append(Path(vault_path) / ".claude" / "settings.json")
        candidates.append(Path(vault_path) / ".claude" / "settings.local.json")

    located = None  # tuple of (config_path, entry_name, entry_dict)
    for c in candidates:
        if not c.exists():
            continue
        try:
            data = json.loads(c.read_text())
        except Exception:
            continue
        servers = data.get("mcpServers", {})
        if "schist" in servers:
            located = (c, "schist", servers["schist"])
            break
        for name, cfg in servers.items():
            args = cfg.get("args", [])
            if any("schist" in str(a) or "dist/index.js" in str(a) for a in args):
                located = (c, name, cfg)
                break
        if located:
            break

    if not located:
        # Also check Cursor as a final fallback.
        cursor = Path.home() / ".cursor" / "mcp.json"
        if cursor.exists():
            try:
                data = json.loads(cursor.read_text())
                servers = data.get("mcpServers", {})
                if "schist" in servers:
                    located = (cursor, "schist", servers["schist"])
            except Exception:
                pass

    if not located:
        return CheckResult(
            "WARN", "MCP", "no schist entry found",
            "Run `schist init --print-mcp-config --identity <name>` and "
            "execute the printed `claude mcp add` command.",
        )

    config_path, entry_name, entry = located
    args = entry.get("args", [])
    args0 = str(args[0]) if args else ""

    warnings: list[str] = []

    # Sub-check 1 (required): args[0] is_file
    if not args0:
        warnings.append("MCP entry has no args[0]")
    elif not Path(args0).is_file():
        warnings.append(f"MCP entry points at {args0} which does not exist")

    # Sub-check 2: env SCHIST_VAULT_PATH matches current vault
    if vault_path and args0:
        env = entry.get("env", {}) or {}
        entry_vault = env.get("SCHIST_VAULT_PATH", "")
        if entry_vault and Path(entry_vault).resolve() != Path(vault_path).resolve():
            warnings.append(
                f"MCP env SCHIST_VAULT_PATH={entry_vault} ≠ current vault {vault_path}"
            )

    # Sub-check 3: args[0] matches auto-detected mcp-server/dist/index.js
    if args0 and Path(args0).is_file():
        detected = _auto_detect_mcp_path()
        if detected and Path(detected).resolve() != Path(args0).resolve():
            warnings.append(
                f"MCP entry's dist/index.js path differs from auto-detected current path"
            )

    if warnings:
        return CheckResult(
            "WARN", "MCP", "; ".join(warnings),
            "Re-run `schist init --print-mcp-config --identity <name>` and "
            "update the entry in your Claude Code config.",
        )

    return CheckResult(
        "PASS", "MCP", f"{entry_name} in {config_path} (args[0]={args0})",
    )
```

- [ ] **Step 3: Run new tests — they should now PASS**

```bash
uv run pytest cli/tests/test_doctor.py::TestCheckMcpConfig -v
```

Expected: all tests in the class PASS.

- [ ] **Step 4: Run the full TestCheckHooksPath again to confirm no cross-impact**

```bash
uv run pytest cli/tests/test_doctor.py -v
```

Expected: all `test_doctor.py` tests pass.

---

## Task 6: Full suite + commit + push + PR

- [ ] **Step 1: Run the full CLI test suite**

```bash
uv run pytest cli/tests/ -v 2>&1 | tail -40
```

Expected: all green. If anything outside `test_doctor.py` fails, STOP and investigate.

- [ ] **Step 2: Commit the implementation**

```bash
git add cli/schist/doctor.py cli/tests/test_doctor.py
git commit -m "$(cat <<'EOF'
feat(doctor): warn on hooksPath override + validate MCP config (#40, #43)

Two additive doctor checks that surface silent-misconfiguration
failure modes from PR #39's review:

#40: check_hooks_path
  Warns when `core.hooksPath` is set on the vault repo. When that
  config is non-default, schist's installed hooks at .git/hooks/ are
  bypassed — post-commit ingest stops firing, the staged-secret guard
  never runs, and nothing surfaces the failure to the user.

#43: check_mcp_config validation
  Replace the too-eager early PASS with three sub-checks aggregated
  into one WARN result:
    1. args[0] exists on disk (REQUIRED)
    2. env.SCHIST_VAULT_PATH matches the current vault
    3. args[0] matches the auto-detected current mcp-server path
  PASS only when all three clear, with args[0] surfaced in the
  detail message so the user can see which entry doctor matched.

Existing TestCheckMcpConfig tests updated to use real on-disk
args[0] paths (the prior /path/to/index.js stub now correctly
WARNs on validation; tests assert continued PASS with a real path).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: Push branch**

```bash
git push -u origin feat/issues-40-43-doctor-checks
```

- [ ] **Step 4: Open PR**

```bash
gh pr create --title "feat(doctor): hooksPath warn + MCP config validation (closes #40, #43)" --body "$(cat <<'EOF'
## Summary

Closes #40 and #43. Two additive doctor checks for silent-misconfiguration failure modes surfaced by PR #39's adversarial review.

## Stacks on PR #51

This branch was created off PR #51 (`refactor/issue-41-spoke-init-staging`) so it inherits the design spec doc that lives on that branch. Once #51 merges, this PR's diff will collapse to just the new check_hooks_path + check_mcp_config enhancement work plus this plan doc.

## Design spec

`docs/superpowers/specs/2026-05-03-spoke-setup-robustness-design.md` (PR B section).

## Behavior change

| Scenario | Before | After |
|---|---|---|
| `core.hooksPath` set to a non-default value | doctor silent; ingest dies and secret guard bypassed unnoticed | doctor WARN with the configured path + fix hint |
| MCP `args[0]` points at a deleted/moved `dist/index.js` | doctor PASS | doctor WARN: "<path> does not exist" |
| MCP `env.SCHIST_VAULT_PATH` ≠ current vault | doctor PASS | doctor WARN |
| MCP `args[0]` ≠ auto-detected current path | doctor PASS | doctor WARN |

No init/sync flow change. Pure additive validation surface.

## Test plan

- [x] `uv run pytest cli/tests/` — full suite green
- [x] `cli/tests/test_doctor.py::TestCheckHooksPath` — 4 new tests
- [x] `cli/tests/test_doctor.py::TestCheckMcpConfig` — 4 new tests + 2 existing updated
- [ ] Manual: in a real vault, `git config core.hooksPath /tmp/elsewhere` then `schist doctor` should print a WARN line for "Hooks path"

## Out of scope

- Issue #50 (MCP context efficiency audit) — separate effort, separate PR.
- Issue #52 (init flow rename-race window) — separate fix.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Verify CI status**

```bash
gh pr view --json state,mergeStateStatus,statusCheckRollup --jq '{state, mergeState: .mergeStateStatus, checks: [.statusCheckRollup[] | {name, conclusion, status}]}'
```

Expected: all checks PASS or in progress (CI may still be running at first check).

- [ ] **Step 6: Hand off to user — do NOT enable `--auto` merge**

This PR depends on #51. The user should merge #51 first, then update-branch this PR (or rebase onto main), then merge.

---

## Self-Review Notes

**Spec coverage:**
- PR B spec section "#40 — check_hooks_path(vault_path)" → Tasks 2 + 3.
- PR B spec section "#43 — check_mcp_config enhancements" → Tasks 4 + 5.
- All seven test cases listed in spec ("#40 set/unset, #43 args[0]/env-mismatch/auto-detect/all-clear/aggregate") → covered across Tasks 2 + 4.

**Type consistency:**
- New helper signature: `_auto_detect_mcp_path() -> Optional[str]`. Used identically in Task 5.
- `check_hooks_path(vault_path: Optional[str]) -> CheckResult` — mirrors existing signatures in doctor.py (e.g. `check_post_commit_hook`).
- CheckResult fields: `status, label, message, fix=None` — confirmed against `cli/schist/doctor.py:25-29`.

**Placeholder scan:** No TBDs. All code blocks runnable. Bash commands exact.

**Cross-impact:** TestRunDoctor::test_full_vault asserts "all vault checks PASS"; the new `Hooks path` check must be added to its `vault_labels` set or it'll fail. Task 3 Step 4 covers this.

**Concerns:**
- The "test_auto_detect_drift_returns_warn" test patches `schist.doctor._auto_detect_mcp_path` — Task 5 must keep that exact symbol name. If the helper gets renamed, the test breaks. Documented in Task 4 Step 2's note.
