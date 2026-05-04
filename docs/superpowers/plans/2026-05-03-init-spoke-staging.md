# init_spoke staging-dir refactor — Implementation Plan (Issue #41)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the half-initialized-spoke failure mode. After a failed `schist init --spoke`, the destination dir must not exist (or must be exactly as it was before — empty / absent), so re-running succeeds without manual `rm -rf`.

**Architecture:** Mirror the existing `_build_standalone_in_staging` pattern at `cli/schist/sync.py:588-616`. Build the spoke inside a sibling staging dir (`<parent>/.<name>.init-<pid>`) and atomically rename to the target only after all six init steps (clone, sparse-checkout, spoke.yaml, `.git/info/exclude`, hooks, rebuild) succeed. SQLite rebuild moves to *after* the rename so the index lives at the user-supplied `db_path` without any path-rewrite gymnastics.

**Tech Stack:** Python 3.12+, pytest, the existing `git_ops` shim, `os.rename` for atomic move (sibling staging guarantees same-FS).

**Design spec:** `docs/superpowers/specs/2026-05-03-spoke-setup-robustness-design.md`

**Out of scope for this plan:** Issues #40 and #43 (doctor.py additions). Those land in a separate PR after this one merges; their plan will be written then.

---

## Task 1: Branch + spec commit

**Files:**
- Create branch: `refactor/issue-41-spoke-init-staging`
- Modify (commit): `docs/superpowers/specs/2026-05-03-spoke-setup-robustness-design.md`
- Modify (commit): `docs/superpowers/plans/2026-05-03-init-spoke-staging.md`

- [ ] **Step 1: Create branch off latest main**

```bash
git checkout main
git pull --ff-only origin main
git checkout -b refactor/issue-41-spoke-init-staging
```

Expected: Switched to a new branch.

- [ ] **Step 2: Commit spec + plan**

```bash
git add docs/superpowers/specs/2026-05-03-spoke-setup-robustness-design.md \
        docs/superpowers/plans/2026-05-03-init-spoke-staging.md
git commit -m "docs: spec + plan for spoke-setup robustness (#41 #40 #43)"
```

Expected: Single commit with two doc files.

---

## Task 2: Add failing test — failure-mid-init leaves no target dir

This is the core behavior change. The test fails today (init_spoke leaves a populated dest dir on partial failure), and is what the staging refactor exists to fix.

**Files:**
- Modify: `cli/tests/test_sync.py` (add to `TestInitSpoke` class, after `test_clone_failure_cleans_up`)

- [ ] **Step 1: Add the failing test**

Insert this method into `class TestInitSpoke` immediately after `test_clone_failure_cleans_up` (currently around line 105-114):

```python
    @patch("schist.sync.git_ops.clone_shallow")
    @patch("schist.sync.git_ops.setup_sparse_checkout",
           return_value=(False, "scope path produces empty checkout"))
    def test_sparse_checkout_failure_leaves_no_target_dir(
        self, mock_sparse, mock_clone, tmp_path, capsys
    ):
        """Issue #41: failure mid-init must leave the target dir absent so
        the user can re-run init without `rm -rf`."""
        from schist.sync import init_spoke

        dest = tmp_path / "spoke"

        # clone_shallow gets called with the staging path, not `dest`.
        # Create whatever directory the function asks for, return success.
        def create_at_arg(hub_url, dest_path, *a, **kw):
            Path(dest_path).mkdir(parents=True, exist_ok=True)
            (Path(dest_path) / ".git" / "info").mkdir(parents=True)
            return True, ""
        mock_clone.side_effect = create_at_arg

        args = MagicMock(
            hub="git@pi:vault.git", scope="bad/scope", identity="cluster",
        )
        with pytest.raises(SystemExit):
            init_spoke(args, str(dest), str(tmp_path / "db.sqlite"))

        # Target dir absent (or empty) — user can re-run.
        assert not dest.exists() or not any(dest.iterdir())

        # No leftover staging dir in the parent.
        leftovers = [
            p for p in tmp_path.iterdir()
            if p.name.startswith(".spoke.init-")
        ]
        assert leftovers == [], f"staging leftovers: {leftovers}"

        assert "sparse checkout failed" in capsys.readouterr().err
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest cli/tests/test_sync.py::TestInitSpoke::test_sparse_checkout_failure_leaves_no_target_dir -v
```

Expected: FAIL. The current `init_spoke` calls `shutil.rmtree(vault_path)` at sparse-checkout failure, but the assertion `not any(dest.iterdir())` may pass coincidentally for sparse-checkout (since today's flow does cleanup at this specific step). Run also with hooks-step injection to confirm the structural problem:

If the sparse-checkout test happens to pass (because today's code coincidentally handles step-2 failure with rmtree), expand the test to inject failure at a *later* step where today's code does NOT clean up — `_install_local_hooks` is the right one. Add a second assertion-style test:

```python
    @patch("schist.sync.git_ops.clone_shallow")
    @patch("schist.sync.git_ops.setup_sparse_checkout", return_value=(True, ""))
    @patch("schist.sync._install_local_hooks", side_effect=OSError("disk full"))
    def test_hook_install_failure_leaves_no_target_dir(
        self, mock_hooks, mock_sparse, mock_clone, tmp_path, capsys
    ):
        """Failure in a step that today has NO cleanup (hooks install) must
        also leave the target dir absent under the staging refactor."""
        from schist.sync import init_spoke

        dest = tmp_path / "spoke"

        def create_at_arg(hub_url, dest_path, *a, **kw):
            Path(dest_path).mkdir(parents=True, exist_ok=True)
            (Path(dest_path) / ".git" / "info").mkdir(parents=True)
            return True, ""
        mock_clone.side_effect = create_at_arg

        args = MagicMock(
            hub="git@pi:vault.git", scope="research/x", identity="cluster",
        )
        with pytest.raises(SystemExit):
            init_spoke(args, str(dest), str(tmp_path / "db.sqlite"))

        assert not dest.exists() or not any(dest.iterdir())
        leftovers = [
            p for p in tmp_path.iterdir()
            if p.name.startswith(".spoke.init-")
        ]
        assert leftovers == []
```

This second test will FAIL on today's code because hooks-install failure leaves a populated dest dir. That's the structural failure the refactor fixes.

```bash
uv run pytest cli/tests/test_sync.py::TestInitSpoke::test_hook_install_failure_leaves_no_target_dir -v
```

Expected: FAIL. Save the failure output — this is what the refactor must fix.

- [ ] **Step 3: Commit the failing tests**

```bash
git add cli/tests/test_sync.py
git commit -m "test(spoke): add failure-mid-init regression tests (#41)"
```

Expected: Tests committed in red state. Per TDD, this is the failing-test landmark.

---

## Task 3: Extract `_build_spoke_in_staging` helper + refactor `init_spoke`

**Files:**
- Modify: `cli/schist/sync.py` — function `init_spoke` (lines 104-162) and add new helper `_build_spoke_in_staging` immediately below it.

- [ ] **Step 1: Add the staging helper**

Insert this new function immediately AFTER the current `init_spoke` (line 163, before `def sync_pull`):

```python
def _build_spoke_in_staging(
    staging: Path,
    hub: str,
    scope: str,
    identity: str,
) -> None:
    """Build the spoke working-tree repo entirely inside `staging`.

    Mirrors `_build_standalone_in_staging` for the spoke flavor: clone,
    sparse-checkout, write spoke.yaml, write .git/info/exclude, install
    hooks. Raises `_InitError` with a descriptive message on any failure;
    caller is responsible for cleaning up `staging`.

    SQLite rebuild is intentionally NOT in this helper — it runs against
    the final vault path AFTER the atomic rename so `db_path` (set by the
    caller against the user-visible vault path) doesn't need rewriting.
    """
    print(f"Cloning from {hub}...")
    ok, output = git_ops.clone_shallow(hub, str(staging))
    if not ok:
        raise _InitError(f"clone failed: {output}")

    print(f"Setting up sparse checkout for scope '{scope}'...")
    ok, output = git_ops.setup_sparse_checkout(str(staging), scope)
    if not ok:
        raise _InitError(f"sparse checkout failed: {output}")

    config = SpokeConfig(hub=hub, identity=identity, scope=scope)
    save_spoke_config(str(staging), config)

    exclude_path = staging / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    with open(exclude_path, "a") as f:
        f.write(f"\n# schist spoke config (never pushed to hub)\n{'.schist/spoke.yaml'}\n")

    _install_local_hooks(str(staging))
```

- [ ] **Step 2: Replace `init_spoke` body with staging dispatch**

Replace the body of `init_spoke` (lines 104-162). New full function:

```python
def init_spoke(args, vault_path: str, db_path: str) -> None:
    """Initialize a spoke vault from hub via shallow clone + sparse checkout.

    Mirrors init_standalone's staging-dir + atomic-rename pattern so a
    half-initialized target dir never exists on disk. On failure, only the
    staging directory is touched and it is cleaned up before exit.
    """
    hub = args.hub
    scope = args.scope
    identity = args.identity

    if not hub:
        print("Error: --hub is required for spoke init", file=sys.stderr)
        sys.exit(1)
    if not scope:
        print("Error: --scope is required for spoke init", file=sys.stderr)
        sys.exit(1)
    if not identity:
        print("Error: --identity is required (or set SCHIST_IDENTITY)", file=sys.stderr)
        sys.exit(1)

    target = Path(vault_path).resolve()
    if target.exists() and any(target.iterdir()):
        print(
            f"Error: directory '{vault_path}' already exists and is not empty",
            file=sys.stderr,
        )
        sys.exit(1)

    # Stage in a sibling so the final os.rename is atomic (same FS).
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.init-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)

    try:
        _build_spoke_in_staging(staging, hub, scope, identity)
    except (_InitError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        try:
            if staging.exists():
                shutil.rmtree(staging)
        except OSError as cleanup_err:
            print(
                f"Warning: could not clean up staging dir {staging}: {cleanup_err}\n"
                f"  Manual fix: rm -rf {staging}",
                file=sys.stderr,
            )
        sys.exit(1)

    # Atomic rename: either target points at a complete spoke, or nothing.
    if target.exists():
        target.rmdir()
    os.rename(staging, target)

    # Rebuild SQLite index against the final path (best-effort post-rename).
    # If this step fails the spoke is still usable; user can re-run rebuild.
    _rebuild_index(str(target), db_path)

    scope_path = target / scope
    file_count = sum(1 for _ in scope_path.rglob("*.md")) if scope_path.exists() else 0
    print(f"Spoke initialized: identity={identity} scope={scope} ({file_count} files)")
```

- [ ] **Step 3: Run the new failure tests — they should now PASS**

```bash
uv run pytest cli/tests/test_sync.py::TestInitSpoke::test_sparse_checkout_failure_leaves_no_target_dir cli/tests/test_sync.py::TestInitSpoke::test_hook_install_failure_leaves_no_target_dir -v
```

Expected: both PASS. If `test_hook_install_failure_leaves_no_target_dir` still fails, the staging cleanup branch isn't being hit — verify the `try/except (_InitError, OSError)` catches the `OSError("disk full")` raised by the mocked `_install_local_hooks`.

---

## Task 4: Update existing happy-path tests to call the new helper signature

The pre-existing `test_creates_spoke_config` and `test_installs_local_hooks` mock `clone_shallow` to create `dest` directly. After the refactor, `clone_shallow` is called with the *staging* path; the mock must create whatever path the function passes.

**Files:**
- Modify: `cli/tests/test_sync.py`

- [ ] **Step 1: Update `test_creates_spoke_config`'s mock**

Find the existing block (around line 84-88):

```python
        # clone_shallow creates the directory
        def create_dir(*a, **kw):
            Path(dest).mkdir(parents=True, exist_ok=True)
            (Path(dest) / ".git" / "info").mkdir(parents=True)
            return True, ""
        mock_clone.side_effect = create_dir
```

Replace with:

```python
        # clone_shallow now receives the staging path (not `dest`); create
        # whatever path the caller passes so the mock works regardless.
        def create_at_arg(hub_url, dest_path, *a, **kw):
            Path(dest_path).mkdir(parents=True, exist_ok=True)
            (Path(dest_path) / ".git" / "info").mkdir(parents=True)
            return True, ""
        mock_clone.side_effect = create_at_arg
```

- [ ] **Step 2: Update `test_installs_local_hooks`'s mock the same way**

Find the analogous block around line 128-132 and apply the same replacement.

- [ ] **Step 3: Run all `TestInitSpoke` tests**

```bash
uv run pytest cli/tests/test_sync.py::TestInitSpoke -v
```

Expected: ALL pass (5 existing + 2 new = 7 tests).

---

## Task 5: Add cleanup-failure-warning test

The refactor's error path prints a "Manual fix: rm -rf" hint when staging cleanup itself fails (e.g., NFS lock, permissions). Add a test for that branch.

**Files:**
- Modify: `cli/tests/test_sync.py` (in `TestInitSpoke`)

- [ ] **Step 1: Add the test**

Insert into `TestInitSpoke` after `test_hook_install_failure_leaves_no_target_dir`:

```python
    @patch("schist.sync.git_ops.clone_shallow")
    @patch("schist.sync.git_ops.setup_sparse_checkout", return_value=(True, ""))
    @patch("schist.sync._install_local_hooks", side_effect=OSError("disk full"))
    @patch("schist.sync.shutil.rmtree", side_effect=OSError("rmtree denied"))
    def test_cleanup_failure_surfaces_manual_fix_hint(
        self, mock_rmtree, mock_hooks, mock_sparse, mock_clone, tmp_path, capsys
    ):
        """When BOTH the build and the cleanup fail, surface a 'Manual fix:
        rm -rf <path>' hint so the user can recover."""
        from schist.sync import init_spoke

        dest = tmp_path / "spoke"

        def create_at_arg(hub_url, dest_path, *a, **kw):
            Path(dest_path).mkdir(parents=True, exist_ok=True)
            (Path(dest_path) / ".git" / "info").mkdir(parents=True)
            return True, ""
        mock_clone.side_effect = create_at_arg

        args = MagicMock(
            hub="git@pi:vault.git", scope="research/x", identity="cluster",
        )
        with pytest.raises(SystemExit):
            init_spoke(args, str(dest), str(tmp_path / "db.sqlite"))

        err = capsys.readouterr().err
        assert "disk full" in err  # original error preserved
        assert "Manual fix: rm -rf" in err  # cleanup-failure hint
```

- [ ] **Step 2: Run the test**

```bash
uv run pytest cli/tests/test_sync.py::TestInitSpoke::test_cleanup_failure_surfaces_manual_fix_hint -v
```

Expected: PASS. If FAIL, confirm `shutil.rmtree` is the symbol being patched (it must match the import in `cli/schist/sync.py:8` — `import shutil` then `shutil.rmtree`, so `schist.sync.shutil.rmtree` is the right patch target).

---

## Task 6: Full suite + commit + push + PR

- [ ] **Step 1: Run full CLI test suite**

```bash
uv run pytest cli/tests/ -v 2>&1 | tail -40
```

Expected: all green. Pay attention to `test_init_standalone.py` (drift guard for hooks; should still pass — we touched neither hook constant) and `test_hub_spoke_e2e.py` (any end-to-end test that exercises real spoke init may need updating if it asserts on intermediate filesystem state).

- [ ] **Step 2: Commit the implementation**

```bash
git add cli/schist/sync.py cli/tests/test_sync.py
git commit -m "$(cat <<'EOF'
fix(spoke): atomic init via sibling staging + atomic rename (#41)

`init_spoke` previously ran six sequential init steps (clone, sparse-
checkout, spoke.yaml, .git/info/exclude, hooks, rebuild) directly
against the destination path. Failure at any step ≥ 2 left a
half-populated dir that the next `schist init --spoke` refused with
"directory already exists and is not empty", forcing a manual
`rm -rf` to recover.

Mirror init_standalone's existing pattern: clone into a sibling
staging dir (`<parent>/.<name>.init-<pid>`, same FS so os.rename is
atomic), run all six configuration steps inside staging, then
atomically rename to the target only on full success. SQLite rebuild
moves to AFTER the rename so it operates against the user-supplied
db_path without any path-rewrite gymnastics.

On any failure the staging dir is `shutil.rmtree`'d and the target
is never created. If the cleanup itself fails (NFS lock, permission
denied), surface a "Manual fix: rm -rf <staging>" hint so the user
can recover.

Tests:
- Failure mid-init (sparse-checkout step) leaves no target / leftover
- Failure in a step today has NO cleanup (hooks install) leaves no
  target / leftover
- Cleanup-failure surfaces the Manual-fix hint (both error preserved)
- Existing happy-path tests updated: clone_shallow mock now creates
  whatever path it receives (was hard-coded to dest).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: Push branch**

```bash
git push -u origin refactor/issue-41-spoke-init-staging
```

- [ ] **Step 4: Open PR**

```bash
gh pr create --title "fix(spoke): atomic init via sibling staging + rename (closes #41)" --body "$(cat <<'EOF'
## Summary

Closes #41. Mirrors `init_standalone`'s staging-dir + atomic-rename pattern for the spoke init flow, eliminating the half-initialized-spoke failure mode.

## Design spec

`docs/superpowers/specs/2026-05-03-spoke-setup-robustness-design.md` (committed in this PR).

## Behavior change

| Scenario | Before | After |
|---|---|---|
| Happy path | Spoke initialized in-place | Spoke built in `<parent>/.<name>.init-<pid>`, atomically renamed to target |
| Step ≥ 2 failure | Target dir half-populated; next `init --spoke` rejects | Target absent; staging cleaned up; re-run works |
| Cleanup also fails | Silent leftover | "Manual fix: rm -rf <staging>" surfaced |

No public-API change. Test surface: 3 new tests + 2 existing-test mock-shape updates.

## Test plan

- [x] `uv run pytest cli/tests/` — full suite green
- [x] `cli/tests/test_sync.py::TestInitSpoke` — 7 tests (5 existing + 2 new failure-mode + 1 cleanup-failure)
- [ ] Manual: `schist init --spoke --hub <hub> --scope nope/garbage --identity test --vault /tmp/test-spoke` should leave `/tmp/test-spoke` absent (smoke test post-merge)

## Out of scope

Issues #40 and #43 follow in PR B (separate plan).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Verify CI status**

```bash
gh pr view --json state,mergeStateStatus,statusCheckRollup --jq '{state, mergeState: .mergeStateStatus, checks: [.statusCheckRollup[] | {name, conclusion, status}]}'
```

Wait until all checks complete. Expected: all PASS. If MCP-server CI fails, that's unrelated to this Python-only change — investigate before re-running.

- [ ] **Step 6: Hand off to user for review + merge**

This PR is a behavior change to a critical path. Do NOT enable `--auto` merge. Hand off and let the user merge after review.

---

## Self-Review Notes

**Spec coverage:**
- PR A spec section ("Issue #41") → Tasks 2-6.
- Test plan items: "failure-mid-init leaves no staging dir" → Task 2 + 3; "cleanup-failure surfaces a Manual fix hint" → Task 5; "happy-path regression" → Task 4 (mock update preserves existing assertions).
- "Re-run init_spoke succeeds" — implicit in Task 2's `not dest.exists() or not any(dest.iterdir())` assertion (an empty/absent target passes the existing non-empty guard, so a re-run would proceed).

**Type consistency:**
- Helper signature: `_build_spoke_in_staging(staging: Path, hub: str, scope: str, identity: str) -> None`. Used identically in Task 3.
- `init_spoke` signature unchanged (`args, vault_path: str, db_path: str`).
- Mock side_effect renamed `create_dir` → `create_at_arg` consistently in Tasks 2 + 4.

**Placeholder scan:** No TBDs / TODOs. All code blocks are complete, all bash commands are runnable.

**Out-of-scope additions confirmed:** PR B (#40 + #43) has its own future plan; SQLite rebuild moves to post-rename intentionally (documented in helper docstring + commit message).
