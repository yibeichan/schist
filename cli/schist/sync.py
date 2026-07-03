"""Spoke sync operations: init, pull, push. Also hub init."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from . import git_ops
from .acl import ACLError, NAME_RE, parse_vault_data
from .spoke_config import SpokeConfig, is_spoke, load_spoke_config, save_spoke_config

# Historical default for the now-deprecated --scope-prefix flag. Retained
# only so init_hub can detect a user-supplied custom value and warn. Keep
# in sync with the argparse default in __main__.py (which imports it).
_SCOPE_PREFIX_LEGACY_DEFAULT = "research"


class _InitError(Exception):
    """Raised by init_* build steps so the outer function can clean up staging."""


PRE_RECEIVE_HOOK = """\
#!/usr/bin/env python3
\"\"\"Git pre-receive hook — enforces vault.yaml ACL on pushes.

Installed by `schist init --hub`. Requires the schist package to be
importable by `python3` on this host
(pip install -e <schist>/cli, or uv pip install --system -e <schist>/cli).
\"\"\"

import sys

from schist.pre_receive import main

sys.exit(main())
"""


POST_COMMIT_HOOK = r"""#!/bin/sh
# schist post-commit hook — re-ingest vault into SQLite after every commit
# schist-hook-version: 4

VAULT_ROOT=$(git rev-parse --show-toplevel)
DB_PATH="$VAULT_ROOT/.schist/schist.db"
mkdir -p "$VAULT_ROOT/.schist"

# Find ingest.py: env var, then common locations
if [ -n "$SCHIST_INGEST_SCRIPT" ] && [ -f "$SCHIST_INGEST_SCRIPT" ]; then
    INGEST="$SCHIST_INGEST_SCRIPT"
elif [ -f "$VAULT_ROOT/.schist/ingest.py" ]; then
    INGEST="$VAULT_ROOT/.schist/ingest.py"
elif command -v schist-ingest >/dev/null 2>&1; then
    schist-ingest --vault "$VAULT_ROOT" --db "$DB_PATH"
    exit $?
else
    echo "schist: ingest.py not found. Set SCHIST_INGEST_SCRIPT or install schist."
    exit 0
fi

python3 "$INGEST" --vault "$VAULT_ROOT" --db "$DB_PATH"
"""


# Bump HOOK_VERSION when you change PRE_COMMIT_HOOK or POST_COMMIT_HOOK so that
# `schist doctor` can detect spokes running stale hook templates. The matching
# `# schist-hook-version: N` line lives inside each hook script body. A user
# who has intentionally customized their hook can replace the version line
# with `# schist-hook-version: pinned` to silence the staleness warning.
HOOK_VERSION = 4

PRE_COMMIT_HOOK = r"""#!/bin/sh
# schist pre-commit hook — reject staged files containing secrets
# schist-hook-version: 4

# Patterns intentionally require a left boundary on token prefixes so substrings
# like "task-..." inside a filename don't trigger on "sk-...", and require a
# quoted value for password/api_key so docs prose ("set `password = <value>`")
# doesn't trip the guard. See issue #103 for the false-positive cases.
PATTERNS="(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}|(^|[^A-Za-z0-9])ghp_[A-Za-z0-9]{20,}|(^|[^A-Za-z0-9])ghs_[A-Za-z0-9]{20,}|(^|[^A-Za-z0-9])AKIA[A-Z0-9]{16}|-----BEGIN [A-Z ]+PRIVATE KEY-----|password\s*=\s*[\"'][^\"' ]+[\"']|api_key\s*=\s*[\"'][^\"' ]+[\"']"

STAGED_FILES=$(mktemp "${TMPDIR:-/tmp}/schist-pre-commit.XXXXXX") || exit 1
trap 'rm -f "$STAGED_FILES"' EXIT HUP INT TERM

git diff --cached --name-only -z --diff-filter=ACMR > "$STAGED_FILES"
if [ ! -s "$STAGED_FILES" ]; then
    exit 0
fi

MATCH=$(xargs -0 -I {} sh -c 'git show ":$1" 2>/dev/null | grep -qE "$2" && printf "%s\n" "$1"' sh {} "$PATTERNS" < "$STAGED_FILES")
if [ -n "$MATCH" ]; then
    echo "ERROR: Potential secret detected in staged files:"
    echo "$MATCH" | sed 's/^/  /'
    echo ""
    echo "If this is intentional, use git commit --no-verify"
    exit 1
fi

exit 0
"""


def _atomic_write_hook(hook_path: Path, body: str) -> None:
    """Write `body` to `hook_path` atomically + executable.

    A naive `write_text` then `chmod` leaves a window where a concurrent git
    invocation could `exec` a half-written shell script. We write to a sibling
    `.tmp` file (same directory ⇒ same filesystem ⇒ rename is atomic on POSIX)
    and `os.replace` over the target.
    """
    tmp = hook_path.with_name(hook_path.name + ".tmp")
    tmp.write_text(body)
    tmp.chmod(0o755)
    os.replace(tmp, hook_path)


def _install_local_hooks(vault_path) -> None:
    """Write the post-commit and pre-commit hooks into a working-tree repo.

    Shared by standalone and spoke init so either flavor of vault gets the
    SQLite auto-rebuild and the staged-secret guard. Caller must have already
    created `<vault>/.git/`. Also called by `schist hooks reinstall` to refresh
    spokes initialized with an older hook template (issue #103).
    """
    hooks_dir = Path(vault_path) / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_hook(hooks_dir / "post-commit", POST_COMMIT_HOOK)
    _atomic_write_hook(hooks_dir / "pre-commit", PRE_COMMIT_HOOK)


def _install_staging_tree(staging: Path, target: Path, display_path: str) -> None:
    """Atomically install a completed staging tree, cleaning staging on failure."""
    try:
        if target.exists():
            target.rmdir()
        os.rename(staging, target)
    except OSError as e:
        print(f"Error: failed to install vault at '{display_path}': {e}", file=sys.stderr)
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


_HOOK_VERSION_LINE = re.compile(r"^# schist-hook-version:\s*(\S+)", re.MULTILINE)


def _hook_pinned(hook_path: Path) -> bool:
    """Return True iff the hook carries a `# schist-hook-version: pinned`
    marker — the opt-out signal users set on intentionally-customized hooks.
    """
    try:
        text = hook_path.read_text()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return False
    m = _HOOK_VERSION_LINE.search(text)
    return bool(m and m.group(1) == "pinned")


def hooks_reinstall(args, vault_path: str, db_path: str) -> None:
    """Re-write pre-commit and post-commit hooks from the canonical templates.

    Refreshes spokes that were init'd before HOOK_VERSION was bumped (issue
    #103). Hooks carrying `# schist-hook-version: pinned` are skipped unless
    `--force` is passed — that marker is the documented opt-out for users who
    intentionally customized their hook bodies.
    """
    target = Path(vault_path)
    if not (target / ".git").exists():
        print(f"Error: {vault_path} is not a git repository", file=sys.stderr)
        sys.exit(1)

    hooks_dir = target / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    force = getattr(args, "force", False)
    skipped = []
    written = []
    for name, body in (("pre-commit", PRE_COMMIT_HOOK), ("post-commit", POST_COMMIT_HOOK)):
        path = hooks_dir / name
        if not force and _hook_pinned(path):
            skipped.append(name)
            continue
        _atomic_write_hook(path, body)
        written.append(name)

    if written:
        print(f"Reinstalled hooks: {', '.join(written)} (template v{HOOK_VERSION})")
    for name in skipped:
        print(
            f"Skipped {name}: marked `# schist-hook-version: pinned`. "
            f"Pass --force to overwrite.",
            file=sys.stderr,
        )


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
    _install_staging_tree(staging, target, vault_path)

    # Rebuild SQLite index against the final path (best-effort post-rename).
    # If this step fails the spoke is still usable; user can re-run rebuild.
    _rebuild_index(str(target), db_path)

    scope_path = target / scope
    file_count = sum(1 for _ in scope_path.rglob("*.md")) if scope_path.exists() else 0
    print(f"Spoke initialized: identity={identity} scope={scope} ({file_count} files)")


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


def _run_git_cleanup(vault_path: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=vault_path,
        capture_output=True,
        text=True,
    )


def _cleanup_rebase_state(vault_path: str) -> None:
    print("Aborting leftover rebase state...", file=sys.stderr)
    abort = _run_git_cleanup(vault_path, ["rebase", "--abort"])
    if abort.returncode == 0:
        return

    # --abort can fail if the rebase is in a weird state (e.g. git is
    # still running, or an orphan rebase-merge without a HEAD ref). Try
    # --quit, which drops rebase state without restoring HEAD.
    quit_result = _run_git_cleanup(vault_path, ["rebase", "--quit"])
    if quit_result.returncode == 0:
        return

    print(
        "Error: could not clear rebase state automatically.\n"
        f"  rebase --abort: {abort.stderr.strip()}\n"
        f"  rebase --quit:  {quit_result.stderr.strip()}\n"
        "  Manual fix: rm -rf .git/rebase-merge .git/rebase-apply",
        file=sys.stderr,
    )
    sys.exit(1)


def _cleanup_merge_state(vault_path: str) -> None:
    print("Aborting leftover merge state...", file=sys.stderr)
    abort = _run_git_cleanup(vault_path, ["merge", "--abort"])
    if abort.returncode == 0:
        return

    print(
        "Error: could not clear merge state automatically.\n"
        f"  merge --abort: {abort.stderr.strip()}\n"
        "  Manual fix: inspect git status, resolve or abort the merge manually, then rerun sync.",
        file=sys.stderr,
    )
    sys.exit(1)


def _cleanup_stale_index_lock(vault_path: str) -> None:
    lock_path = Path(vault_path) / ".git" / "index.lock"
    if not lock_path.exists():
        return
    print("Removing stale git index.lock...", file=sys.stderr)
    try:
        lock_path.unlink()
    except OSError as e:
        print(
            "Error: could not remove stale .git/index.lock automatically.\n"
            f"  {e}\n"
            "  Manual fix: ensure no git process is running, then remove .git/index.lock.",
            file=sys.stderr,
        )
        sys.exit(1)


def cleanup_stale_git_state(vault_path: str, *, force: bool) -> None:
    """Clear stale git operation sentinels before sync.

    Rebase state is always cleaned because a killed `sync pull` leaves the
    spoke permanently unusable until it is removed. Merge and index-lock cleanup
    are gated behind --force: merge abort can touch the worktree, and lock files
    should only be removed as an explicit recovery action. Under --force, remove
    index.lock before aborting a merge so Git gets a fair chance to restore the
    worktree. Never drop MERGE_* sentinels after abort failure; that can hide
    unresolved merge content from later status/staging checks.
    """
    git_dir = Path(vault_path) / ".git"
    rebase_present = any((git_dir / d).exists() for d in ("rebase-merge", "rebase-apply"))
    if rebase_present:
        _cleanup_rebase_state(vault_path)

    merge_present = (git_dir / "MERGE_HEAD").exists()
    index_lock_present = (git_dir / "index.lock").exists()
    if index_lock_present:
        if not force:
            print(
                "Error: git index.lock present. Run `schist sync --force` after "
                "confirming no git process is active.",
                file=sys.stderr,
            )
            sys.exit(1)
        _cleanup_stale_index_lock(vault_path)

    if merge_present:
        if not force:
            print(
                "Error: merge in progress. Run `schist sync --force` after "
                "confirming no manual merge is active.",
                file=sys.stderr,
            )
            sys.exit(1)
        _cleanup_merge_state(vault_path)


def _force_enabled(args) -> bool:
    return getattr(args, "force", False) is True


def sync_pull(args, vault_path: str, db_path: str) -> None:
    """Pull updates from hub and rebuild SQLite index."""
    if not is_spoke(vault_path):
        print("Error: not a spoke vault (missing .schist/spoke.yaml)", file=sys.stderr)
        sys.exit(1)

    config = load_spoke_config(vault_path)
    cleanup_stale_git_state(vault_path, force=_force_enabled(args))

    print(f"Pulling from hub as {config.identity}...")

    ok, output = git_ops.pull_rebase(vault_path)
    if not ok:
        if "CONFLICT" in output or "conflict" in output.lower():
            _print_conflict_recovery(vault_path, config, output)
        else:
            print(f"Error: pull failed — {output}", file=sys.stderr)
        sys.exit(1)

    # Rebuild index
    _rebuild_index(vault_path, db_path)
    print(f"Pull complete. Index rebuilt.")


_CONFLICT_RE = re.compile(r"CONFLICT \([^)]+\): Merge conflict in (.+)")


def _extract_conflicting_files(git_output: str) -> list[str]:
    """Parse git rebase output for `CONFLICT (content): Merge conflict in <path>`
    lines. Returns the list of conflicting file paths, in order of appearance,
    deduplicated. Returns an empty list if no conflicts were matched (e.g. the
    failure was a delete-modify conflict that git phrases differently)."""
    seen: set[str] = set()
    files: list[str] = []
    for line in git_output.splitlines():
        match = _CONFLICT_RE.match(line.strip())
        if match:
            path = match.group(1).strip()
            if path not in seen:
                seen.add(path)
                files.append(path)
    return files


def _print_conflict_recovery(
    vault_path: str, config: SpokeConfig, git_output: str
) -> None:
    """Render the pull-conflict error block with concrete recovery steps.

    `pull_rebase` in git_ops.py auto-aborts the failed rebase, so by the time
    we land here the local working tree is already back to pre-pull state.
    The user's work is NOT lost — they just couldn't automatically absorb the
    hub's changes. This message tells them that explicitly and gives three
    concrete recovery paths sized from safest to most-hands-on."""
    conflicts = _extract_conflicting_files(git_output)

    print(
        "Error: pull failed with conflicts. Local state is unchanged "
        "(the rebase was auto-aborted).",
        file=sys.stderr,
    )
    if conflicts:
        print("", file=sys.stderr)
        print("Conflicting files:", file=sys.stderr)
        for f in conflicts:
            print(f"  {f}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "The hub has commits that touch the same lines as your local work. "
        "Recovery options:",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print("  1. INSPECT what's on the hub first:", file=sys.stderr)
    print(f"       git -C {vault_path} fetch origin", file=sys.stderr)
    print(f"       git -C {vault_path} log HEAD..origin/main --oneline", file=sys.stderr)
    print("", file=sys.stderr)
    print("  2. MANUAL REBASE (advanced — resolve conflicts yourself):", file=sys.stderr)
    print(
        f"       git -C {vault_path} fetch origin && "
        f"git -C {vault_path} rebase origin/main",
        file=sys.stderr,
    )
    print(
        "       # edit conflicting files, git add <files>, git rebase --continue",
        file=sys.stderr,
    )
    print(f"       schist sync pull   # rebuild index", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "  3. RE-CLONE (safest — your local changes must already be pushed):",
        file=sys.stderr,
    )
    print(f"       schist sync push              # push first if you have any", file=sys.stderr)
    print(f"       rm -rf {vault_path}", file=sys.stderr)
    print(
        f"       schist init --spoke --hub {config.hub} "
        f"--scope {config.scope} --identity {config.identity}",
        file=sys.stderr,
    )


def sync_push(args, vault_path: str, db_path: str) -> None:
    """Push local changes to hub."""
    if not is_spoke(vault_path):
        print("Error: not a spoke vault (missing .schist/spoke.yaml)", file=sys.stderr)
        sys.exit(1)

    config = load_spoke_config(vault_path)
    cleanup_stale_git_state(vault_path, force=_force_enabled(args))

    # Auto-commit if there are uncommitted changes
    if git_ops.has_uncommitted_changes(vault_path):
        ok, output = git_ops.stage_scope_files(vault_path, config.scope)
        if not ok:
            print(f"Error: failed to stage scope '{config.scope}': {output}", file=sys.stderr)
            sys.exit(1)

        # Count staged files
        import subprocess

        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=vault_path, capture_output=True, text=True,
        )
        staged = [f for f in result.stdout.strip().split("\n") if f]
        n = len(staged)

        if n > 0:
            msg = f"sync({config.identity}): {n} file{'s' if n != 1 else ''}"
            ok, output = git_ops.commit(vault_path, msg, files=staged)
            if not ok:
                print(f"Error: commit failed: {output}", file=sys.stderr)
                sys.exit(1)
            print(f"Committed {n} file{'s' if n != 1 else ''}")

    # Push
    if not git_ops.has_unpushed_commits(vault_path):
        print("Nothing to push — local is up to date with hub.")
        return

    print(f"Pushing as {config.identity}...")
    ok, output = git_ops.push(vault_path)
    if not ok:
        if "REJECTED" in output.upper() or "rejected" in output:
            print(f"Push rejected by hub:\n{output}", file=sys.stderr)
        elif "Could not resolve" in output or "fatal:" in output:
            print(f"Hub unreachable — changes saved locally. Push when network available.", file=sys.stderr)
            print(f"  Detail: {output}", file=sys.stderr)
        else:
            print(f"Push failed: {output}", file=sys.stderr)
        sys.exit(1)

    print("Pushed to hub.")


def init_hub(args, hub_path: str) -> None:
    """Initialize a bare hub repo with a seeded vault.yaml and pre-receive hook.

    Creates: <hub_path> (bare repo), <hub_path>/hooks/pre-receive (ACL enforcer),
    and an initial commit containing vault.yaml on the main branch.

    Builds everything in a sibling staging directory and atomically renames on
    success. If any step fails, only the staging directory is touched — the
    final hub_path never exists in a half-initialized state, and retries after
    failure always work cleanly.
    """
    name = getattr(args, "name", None)
    participants = list(getattr(args, "participant", None) or [])

    if not name:
        print("Error: --name is required for hub init", file=sys.stderr)
        sys.exit(1)
    if not participants:
        print("Error: at least one --participant is required", file=sys.stderr)
        sys.exit(1)

    hub = Path(hub_path).resolve()
    if hub.exists() and any(hub.iterdir()):
        print(f"Error: hub path '{hub_path}' already exists and is not empty", file=sys.stderr)
        sys.exit(1)

    # Build vault.yaml data and validate before touching the filesystem.
    vault_data = _build_seed_vault(name, participants)
    try:
        parse_vault_data(vault_data)
    except ACLError as e:
        print(f"Error: generated vault.yaml failed validation: {e}", file=sys.stderr)
        sys.exit(1)

    # Stage in a sibling directory so the final os.rename is atomic (same FS).
    # The staging name uses PID to avoid collisions with concurrent inits.
    hub.parent.mkdir(parents=True, exist_ok=True)
    staging = hub.parent / f".{hub.name}.init-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)

    try:
        _build_hub_in_staging(staging, hub, vault_data, participants, name)
    except (_InitError, OSError) as e:
        # OSError catches filesystem failures (permission denied, disk full,
        # etc.) raised by staging.mkdir() or any subsequent write so the user
        # gets a clean "Error: ..." line instead of a raw traceback.
        print(f"Error: {e}", file=sys.stderr)
        # Best-effort staging cleanup. If it fails, surface the path so the
        # user can remove it manually — but the final hub_path is untouched.
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

    # Atomic rename: either hub_path points at a complete bare repo, or it
    # doesn't exist at all. No half-initialized intermediate state.
    _install_staging_tree(staging, hub, hub_path)

    print(f"Hub initialized at {hub_path}")
    print(f"  participants: {', '.join(participants)}")
    print()
    print("Next steps:")
    print(f"  1. Edit {hub_path}/hooks/pre-receive's environment if needed")
    print(f"  2. On each spoke, run: schist init --spoke --hub <url> --scope <scope> --identity <name>")
    print(f"  3. Review and extend vault.yaml by cloning, editing, and pushing.")


def _build_hub_in_staging(
    staging: Path,
    final_hub: Path,
    vault_data: dict,
    participants: list[str],
    name: str,
) -> None:
    """Build the bare repo + hook + seed commit entirely inside `staging`.

    Raises _InitError with a descriptive message on any failure. The caller
    is responsible for cleaning up `staging`.
    """
    # 1. Create bare repo at the staging path
    staging.mkdir(parents=True)
    result = subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(staging)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise _InitError(f"git init --bare failed: {result.stderr.strip()}")

    # 2. Install pre-receive hook
    hook_path = staging / "hooks" / "pre-receive"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(PRE_RECEIVE_HOOK)
    hook_path.chmod(0o755)

    # 3. Sanity-check: schist must be importable for the hook to work at
    #    push time. This runs in the current shell's env, which may differ
    #    from the sshd env the hook actually runs under — warn, don't fail.
    check = subprocess.run(
        ["python3", "-c", "import schist.pre_receive"],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        print(
            "Warning: `python3 -c 'import schist.pre_receive'` failed on this host.\n"
            "  The pre-receive hook will reject all pushes until the schist\n"
            "  package is installed on the path that git-receive-pack uses\n"
            "  (check sshd/gitolite env, not just your interactive shell):\n"
            "  pip install -e <schist>/cli   (or: uv pip install --system -e <schist>/cli)",
            file=sys.stderr,
        )

    # 4. Seed the initial commit via a temp worktree next to staging, so its
    #    git push targets the bare via a same-filesystem absolute path.
    with tempfile.TemporaryDirectory(prefix="schist-hub-seed-") as tmp:
        seed = Path(tmp) / "seed"
        seed.mkdir()
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "schist")
        env.setdefault("GIT_AUTHOR_EMAIL", "schist@local")
        env.setdefault("GIT_COMMITTER_NAME", "schist")
        env.setdefault("GIT_COMMITTER_EMAIL", "schist@local")
        # Pre-receive identity check runs before ACL load — on the fresh bare
        # the ACL returns None (HEAD doesn't exist yet) and the push is
        # allowed, but identity must still be set. Use the first participant.
        env["SCHIST_IDENTITY"] = participants[0]

        def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
            return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)

        for cmd in (
            ["git", "init", "--initial-branch=main"],
            ["git", "remote", "add", "origin", str(staging)],
        ):
            r = run(cmd, seed)
            if r.returncode != 0:
                raise _InitError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")

        (seed / "vault.yaml").write_text(
            yaml.dump(vault_data, default_flow_style=False, sort_keys=False)
        )

        for cmd in (
            ["git", "add", "vault.yaml"],
            ["git", "commit", "-m", f"init: seed vault {name}"],
            ["git", "push", "-u", "origin", "main"],
        ):
            r = run(cmd, seed)
            if r.returncode != 0:
                raise _InitError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")


def _build_seed_vault(name: str, participants: list[str]) -> dict:
    """Construct a minimal valid vault.yaml data dict for the hub seed commit.

    Every participant gets `default_scope: global` and a content-axis write
    list. Authorship is recorded in note frontmatter via `source_agent`, not
    in directory placement — see schema/SCHEMA.md and ADR-002 in the vault.
    Hub operators can broaden specific participants (e.g. a privileged spoke
    that manages `shared/skills/`) by editing vault.yaml after init.
    """
    # Participant write-grants. Intentionally a subset of cli/schist/default.yaml's
    # directory list: `logs/` is infra-owned (rate-limit DB, audit records) and
    # `projects/` is per-installation, so neither is a default participant write
    # target. This is an ACL grant list, NOT the note-bearing-dirs list — the two
    # are semantically distinct and deliberately not coupled.
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


def init_standalone(args) -> None:
    """Initialize a local standalone vault with no hub or participants.

    Scaffolds: a git working repo at the target path with vault.yaml, empty
    notes/concepts/papers directories (each with .gitkeep), a .gitignore that
    excludes .schist/ (runtime SQLite state), and the post-commit + pre-commit
    hooks installed in .git/hooks/ (after the seed commit, so the hooks don't
    run on it).

    Mirrors init_hub's staging-dir + atomic rename pattern so a half-initialized
    target never exists on disk. On failure, only the staging directory is
    touched and it is cleaned up before exit.
    """
    path_arg = getattr(args, "path", None) or "."
    target = Path(path_arg).resolve()

    name = getattr(args, "name", None) or target.name
    identity = getattr(args, "identity", None) or "local"

    if not NAME_RE.match(identity):
        print(
            f"Error: --identity '{identity}' must match ^[a-z][a-z0-9-]*$",
            file=sys.stderr,
        )
        sys.exit(1)

    if target.exists() and any(target.iterdir()):
        print(
            f"Error: '{path_arg}' already exists and is not empty",
            file=sys.stderr,
        )
        sys.exit(1)

    # Build vault.yaml data and validate before touching the filesystem.
    vault_data = _build_standalone_vault(name, identity)
    try:
        parse_vault_data(vault_data)
    except ACLError as e:
        print(
            f"Error: generated vault.yaml failed validation: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Stage in a sibling directory so the final os.rename is atomic (same FS).
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.init-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)

    try:
        _build_standalone_in_staging(staging, vault_data, name)
    except (_InitError, OSError) as e:
        # OSError catches filesystem failures (permission denied, disk full,
        # etc.) raised by staging.mkdir() or any subsequent write so the user
        # gets a clean "Error: ..." line instead of a raw traceback.
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

    # Atomic rename: either target points at a complete vault, or it doesn't
    # exist at all.
    _install_staging_tree(staging, target, path_arg)

    print(f"Vault initialized at {target}")
    print()
    print("Next steps:")
    print(f"  1. export SCHIST_VAULT_PATH={target}")
    print(f"  2. schist add --title 'My first note'")
    print(f"  3. Set SCHIST_INGEST_SCRIPT for post-commit ingest, or install schist-ingest on PATH.")


def _build_standalone_in_staging(
    staging: Path,
    vault_data: dict,
    name: str,
) -> None:
    """Build the standalone working-tree repo entirely inside `staging`.

    Raises _InitError with a descriptive message on any failure. The caller is
    responsible for cleaning up `staging`.
    """
    staging.mkdir(parents=True)

    result = subprocess.run(
        ["git", "init", "--initial-branch=main", str(staging)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise _InitError(f"git init failed: {result.stderr.strip()}")

    # Scaffold empty content dirs with .gitkeep sentinels so they survive the
    # initial commit even though git doesn't track empty directories.
    for d in ("notes", "concepts", "papers"):
        sub = staging / d
        sub.mkdir()
        (sub / ".gitkeep").write_text("")

    # Runtime SQLite state lives under .schist/ and must never be committed.
    (staging / ".gitignore").write_text(".schist/\n")

    (staging / "vault.yaml").write_text(
        yaml.dump(vault_data, default_flow_style=False, sort_keys=False)
    )

    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "schist")
    env.setdefault("GIT_AUTHOR_EMAIL", "schist@local")
    env.setdefault("GIT_COMMITTER_NAME", "schist")
    env.setdefault("GIT_COMMITTER_EMAIL", "schist@local")

    def run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=staging, env=env, capture_output=True, text=True)

    # Seed commit runs BEFORE installing hooks so the commit is unaffected by
    # the pre-commit secret scanner (benign here, but keeps intent explicit).
    for cmd in (
        ["git", "add", "."],
        ["git", "commit", "-m", f"init: scaffold standalone vault {name}"],
    ):
        r = run(cmd)
        if r.returncode != 0:
            raise _InitError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")

    _install_local_hooks(staging)


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


def _print_mcp_config(args) -> None:
    """Print a ready-to-paste MCP server config. Does not create files."""
    import json as _json

    vault_path = args.vault or os.environ.get("SCHIST_VAULT_PATH")
    if not vault_path:
        print("Error: --vault or SCHIST_VAULT_PATH required for --print-mcp-config",
              file=sys.stderr)
        sys.exit(1)
    vault_path = os.path.abspath(vault_path)

    identity = getattr(args, "identity", None) or os.environ.get("SCHIST_IDENTITY", "")

    # Find mcp-server/dist/index.js
    mcp_path = getattr(args, "mcp_server_path", None)
    if not mcp_path:
        # Auto-detect from source checkout
        pkg_dir = Path(__file__).resolve().parents[2]
        candidate = pkg_dir / "mcp-server" / "dist" / "index.js"
        if candidate.exists():
            mcp_path = str(candidate)
    if not mcp_path:
        print("Error: cannot find mcp-server/dist/index.js. "
              "Use --mcp-server-path to specify.", file=sys.stderr)
        sys.exit(1)
    mcp_path = os.path.abspath(mcp_path)

    # Both env vars are needed: SCHIST_AGENT_ID gates MCP write tools (memory
    # ownership), SCHIST_IDENTITY is what the hub's pre-receive hook reads
    # when the MCP server's auto-push fires. Set both when an identity is
    # provided so the user doesn't have to discover the second one later.
    env = {"SCHIST_VAULT_PATH": vault_path}
    if identity:
        env["SCHIST_AGENT_ID"] = identity
        env["SCHIST_IDENTITY"] = identity

    fmt = getattr(args, "mcp_format", "claude")
    if fmt == "claude":
        # Claude Code stores user-scope MCP servers in ~/.claude.json (not the
        # ~/.claude/settings.json file that Claude Desktop uses). The CLI is
        # the supported way to register one — emit a copy-paste-runnable
        # `claude mcp add` command instead of raw JSON.
        # Shell-quote each value so paths/identities containing spaces or
        # metacharacters survive copy-paste-run (e.g. macOS
        # '~/Library/Application Support/...').
        env_flags = " ".join("-e " + shlex.quote(f"{k}={v}") for k, v in env.items())
        print("# Run to register schist with Claude Code (user scope):")
        print(f"claude mcp add --scope user schist {env_flags} -- node {shlex.quote(mcp_path)}")
        # Fallback for older Claude Code CLIs that predate `mcp add` /
        # `--scope` — emit a commented JSON block the user can hand-merge
        # under the top-level `mcpServers` key in ~/.claude.json. See #42.
        fallback = {
            "mcpServers": {
                "schist": {
                    "command": "node",
                    "args": [mcp_path],
                    "env": env,
                }
            }
        }
        print()
        print("# Fallback (older Claude Code without `mcp add`):")
        print("# merge under `mcpServers` in ~/.claude.json")
        for line in _json.dumps(fallback, indent=2).splitlines():
            print(f"# {line}")
    elif fmt == "cursor":
        config = {
            "mcpServers": {
                "schist": {
                    "command": "node",
                    "args": [mcp_path],
                    "env": env,
                }
            }
        }
        print("# Paste into .cursor/mcp.json in your project:")
        print(_json.dumps(config, indent=2))
    else:
        print(f"Error: unknown --format value '{fmt}'", file=sys.stderr)
        sys.exit(1)


def _dispatch_init(args) -> None:
    """Route `schist init` to the right mode based on arg combination.

    Three modes (mutually exclusive):
      - hub:        `--hub-path PATH` (optionally --name/--participant)
      - spoke:      `--spoke --hub URL --scope S --identity I`
      - standalone: no mode flags; optional positional <path>

    Centralizing the conflict matrix here also closes a pre-existing trap:
    `--hub URL` without `--spoke` used to fall through to a KeyError crash,
    and with standalone init added would silently run as standalone and drop
    the hub URL. Both are rejected up front.
    """
    if getattr(args, "print_mcp_config", False):
        _print_mcp_config(args)
        return
    hub_path = getattr(args, "hub_path", None)
    spoke = getattr(args, "spoke", False)
    hub_url = getattr(args, "hub", None)
    standalone_path = getattr(args, "path", None)

    # --scope-prefix is deprecated and ignored in every init mode (flat
    # convention has no per-participant scope prefix). Warn once here, before
    # dispatch, so spoke/standalone/hub all surface it consistently.
    if getattr(args, "scope_prefix", None) not in (None, _SCOPE_PREFIX_LEGACY_DEFAULT):
        print(
            "Warning: --scope-prefix is deprecated and has no effect. "
            "New vaults use scope_convention: flat; authorship is recorded in "
            "the source_agent frontmatter, not via per-participant directories.",
            file=sys.stderr,
        )

    if hub_path and spoke:
        print("Error: --hub-path and --spoke are mutually exclusive", file=sys.stderr)
        sys.exit(1)
    if hub_path and hub_url:
        print("Error: --hub-path and --hub are mutually exclusive (--hub is for spoke mode)", file=sys.stderr)
        sys.exit(1)
    if hub_url and not spoke:
        print("Error: --hub requires --spoke", file=sys.stderr)
        sys.exit(1)
    if standalone_path and (hub_path or spoke):
        print("Error: positional <path> is only valid for standalone init", file=sys.stderr)
        sys.exit(1)

    if hub_path:
        init_hub(args, hub_path)
        return

    if spoke:
        vault_path = args.vault or os.path.basename(hub_url or "vault").removesuffix(".git")
        db_path = args.db or os.path.join(vault_path, ".schist", "schist.db")
        init_spoke(args, vault_path, db_path)
        return

    init_standalone(args)


def _wal_siblings(db: Path) -> tuple[Path, Path]:
    """The -wal/-shm files SQLite pairs with `db` (WAL mode, see #254)."""
    return Path(f"{db}-wal"), Path(f"{db}-shm")


def _unlink_db_with_wal(db: Path) -> None:
    """Remove a SQLite DB together with any -wal/-shm siblings."""
    for p in (db, *_wal_siblings(db)):
        p.unlink(missing_ok=True)


def _move_db_with_wal(src: Path, dst: Path) -> None:
    """Rename a SQLite DB together with any -wal/-shm siblings, atomically.

    SQLite derives the WAL file name from the DB file name, so moving only
    the main file divorces it from its WAL. If a close-checkpoint was ever
    blocked (an MCP reader open at ingest-close time), the -wal holds the
    live data and the moved main file alone reads as EMPTY — and a stray
    -wal left at any path is silently replayed into whatever main DB later
    appears there. So the three names must move as a unit.

    rename() is per-file atomic but the group is not: if one rename fails
    partway (e.g. a transient NFS error on the HPC spokes this targets), a
    naive sequence would leave a half-moved set — the exact stray-wal state
    that causes silent corruption. So on any failure we roll every completed
    move back to `src` and re-raise, leaving the source set intact. The main
    file moves LAST and reverts FIRST, so a `-wal` never sits at a path whose
    main DB is absent except transiently within this function.
    """
    done: list[tuple[Path, Path]] = []
    try:
        for s, d in zip(_wal_siblings(src), _wal_siblings(dst)):
            if s.exists():
                s.rename(d)
                done.append((s, d))
        src.rename(dst)
    except OSError:
        for s, d in reversed(done):
            try:
                d.rename(s)
            except OSError:
                pass
        raise


def _rebuild_index(vault_path: str, db_path: str) -> None:
    """Delete existing SQLite and re-ingest from markdown files.

    Uses rename-on-success: old DB is only removed after ingest succeeds.
    Before dropping the backup, the `concept_aliases` side table is copied
    forward into the new DB — it lives alongside docs/concepts/edges in
    schist.db but is not rebuilt from markdown, so on the commit-path
    rebuild it survives naturally (ingest.py runs against the existing DB).
    Here we rename the whole file, so the copy is the only thing keeping
    its rows alive.

    All file moves/removals go through the WAL-aware helpers above; with the
    DB in WAL mode (#254) the -wal sibling can hold the entire index, and
    handling only the main file silently loses or corrupts it.
    """
    db = Path(db_path)
    backup = None
    if db.exists():
        backup = db.with_suffix(".db.bak")
        # Clear any stale backup first — a leftover .bak-wal from an earlier
        # crashed rebuild would otherwise pair with the fresh backup and be
        # replayed into it.
        _unlink_db_with_wal(backup)
        try:
            _move_db_with_wal(db, backup)
        except OSError as e:
            # The backup move rolled itself back, so the existing index is
            # still intact at db_path. Abort rather than proceed to an ingest
            # that would overwrite it with no recoverable backup.
            print(f"Warning: index rebuild skipped (backup failed): {e}", file=sys.stderr)
            return

    from .sqlite_query import _run_ingest

    try:
        _run_ingest(vault_path, db_path)
        if backup and backup.exists():
            _preserve_side_tables(backup, db)
            _unlink_db_with_wal(backup)
    except Exception as e:
        # Restore backup so user keeps old (stale) index rather than none.
        # Remove anything the failed ingest left at the live name first so
        # its -wal can't be replayed into the restored backup.
        if backup and backup.exists():
            _unlink_db_with_wal(db)
            try:
                _move_db_with_wal(backup, db)
            except OSError as restore_err:
                # Restore itself failed but rolled back, so the good index is
                # still at the backup path — tell the user where it is rather
                # than dying with a traceback.
                print(
                    f"Warning: index restore failed; last-good DB left at {backup}: {restore_err}",
                    file=sys.stderr,
                )
        print(f"Warning: index rebuild failed: {e}", file=sys.stderr)


# Side tables that live in schist.db but are not populated by ingest.py.
# Columns are hardcoded (rather than using `SELECT *`) so a schema evolution
# in `cli/schist/schema.sql` cannot silently misalign columns during the copy.
# If you add a column to any listed table, update this map too — `test_sync.py`
# has a guard test that asserts every table column is listed here.
_SIDE_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "concept_aliases": (
        "duplicate_slug", "canonical_slug", "reason", "created_by", "created_at",
    ),
}


def _preserve_side_tables(backup: Path, new_db: Path) -> None:
    """Copy side-table rows from `backup` into `new_db`.

    Uses ATTACH DATABASE + INSERT OR IGNORE SELECT with explicit column lists
    so schema drift between backup and new DB can't cause column-order
    corruption. A missing table in the backup (older DB format) is skipped
    silently; any other sqlite3 error re-raises.
    """
    import sqlite3

    conn = sqlite3.connect(str(new_db))
    try:
        conn.execute("ATTACH DATABASE ? AS backup", (str(backup),))
        for table, cols in _SIDE_TABLE_COLUMNS.items():
            col_list = ", ".join(cols)
            try:
                conn.execute(
                    f"INSERT OR IGNORE INTO main.{table} ({col_list}) "
                    f"SELECT {col_list} FROM backup.{table}"
                )
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e).lower():
                    raise
        # Prune aliases whose endpoints no longer exist in the freshly
        # rebuilt concepts table. The backup may carry dangling rows (e.g.
        # written before #198 FK enforcement, or orphaned when a concept was
        # later deleted); copying them forward unchanged would reintroduce the
        # dangling-FK rows that ingest.py prunes on the in-place commit path.
        # Mirror that DELETE here so the rebuild / spoke-pull path converges to
        # the same state and existing corruption is backfilled, not propagated.
        # See issue #213.
        try:
            conn.execute(
                """
                DELETE FROM concept_aliases
                WHERE duplicate_slug NOT IN (SELECT slug FROM concepts)
                   OR canonical_slug NOT IN (SELECT slug FROM concepts)
                """
            )
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                raise
        conn.commit()
    finally:
        conn.close()
