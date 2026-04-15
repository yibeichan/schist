"""Spoke sync operations: init, pull, push. Also hub init."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from . import git_ops
from .acl import ACLError, NAME_RE, parse_vault_data
from .spoke_config import SpokeConfig, is_spoke, load_spoke_config, save_spoke_config

class _InitError(Exception):
    """Raised by init_* build steps so the outer function can clean up staging."""


PRE_RECEIVE_HOOK = """\
#!/usr/bin/env python3
\"\"\"Git pre-receive hook — enforces vault.yaml ACL on pushes.

Installed by `schist init --hub`. Requires the schist package to be
importable by `python3` on this host (pip install -e <schist>/cli).
\"\"\"

import sys

from schist.pre_receive import main

sys.exit(main())
"""


POST_COMMIT_HOOK = r"""#!/bin/sh
# schist post-commit hook — re-ingest vault into SQLite after every commit

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


PRE_COMMIT_HOOK = r"""#!/bin/sh
# schist pre-commit hook — reject staged files containing secrets

PATTERNS='sk-|ghp_|ghs_|AKIA|-----BEGIN|password\s*=|api_key\s*='

STAGED_FILES=$(git diff --cached --name-only)
if [ -z "$STAGED_FILES" ]; then
    exit 0
fi

MATCH=$(echo "$STAGED_FILES" | xargs grep -lE "$PATTERNS" 2>/dev/null)
if [ -n "$MATCH" ]; then
    echo "ERROR: Potential secret detected in staged files:"
    echo "$MATCH" | sed 's/^/  /'
    echo ""
    echo "If this is intentional, use git commit --no-verify"
    exit 1
fi

exit 0
"""


def init_spoke(args, vault_path: str, db_path: str) -> None:
    """Initialize a spoke vault from hub via shallow clone + sparse checkout."""
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

    if Path(vault_path).exists() and any(Path(vault_path).iterdir()):
        print(f"Error: directory '{vault_path}' already exists and is not empty", file=sys.stderr)
        sys.exit(1)

    # Step 1: Shallow clone (no checkout)
    print(f"Cloning from {hub}...")
    ok, output = git_ops.clone_shallow(hub, vault_path)
    if not ok:
        print(f"Error: clone failed: {output}", file=sys.stderr)
        # Clean up partial clone
        if Path(vault_path).exists():
            shutil.rmtree(vault_path)
        sys.exit(1)

    # Step 2: Sparse checkout for scope
    print(f"Setting up sparse checkout for scope '{scope}'...")
    ok, output = git_ops.setup_sparse_checkout(vault_path, scope)
    if not ok:
        print(f"Error: sparse checkout failed: {output}", file=sys.stderr)
        shutil.rmtree(vault_path)
        sys.exit(1)

    # Step 3: Write spoke config
    config = SpokeConfig(hub=hub, identity=identity, scope=scope)
    save_spoke_config(vault_path, config)

    # Step 4: Exclude spoke config from git
    exclude_path = Path(vault_path) / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    with open(exclude_path, "a") as f:
        f.write(f"\n# schist spoke config (never pushed to hub)\n{'.schist/spoke.yaml'}\n")

    # Step 5: Rebuild SQLite index
    _rebuild_index(vault_path, db_path)

    # Summary
    scope_path = Path(vault_path) / scope
    file_count = sum(1 for _ in scope_path.rglob("*.md")) if scope_path.exists() else 0
    print(f"Spoke initialized: identity={identity} scope={scope} ({file_count} files)")


def sync_pull(args, vault_path: str, db_path: str) -> None:
    """Pull updates from hub and rebuild SQLite index."""
    if not is_spoke(vault_path):
        print("Error: not a spoke vault (missing .schist/spoke.yaml)", file=sys.stderr)
        sys.exit(1)

    config = load_spoke_config(vault_path)

    # Self-heal: a prior pull may have been SIGKILL'd mid-rebase (e.g. by the
    # MCP server's 5s maybeSpokePull timeout). Detect leftover rebase state
    # and abort before starting a fresh pull. Without this, every subsequent
    # sync fails with "rebase in progress" until manual cleanup.
    rebase_present = any(
        (Path(vault_path) / d).exists()
        for d in (".git/rebase-merge", ".git/rebase-apply")
    )
    if rebase_present:
        print("Aborting leftover rebase state...", file=sys.stderr)
        abort = subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=vault_path, capture_output=True, text=True,
        )
        if abort.returncode != 0:
            # --abort can fail if the rebase is in a weird state (e.g. git is
            # still running, or an orphan rebase-merge without a HEAD ref).
            # Try --quit, which drops rebase state without restoring HEAD.
            quit_result = subprocess.run(
                ["git", "rebase", "--quit"],
                cwd=vault_path, capture_output=True, text=True,
            )
            if quit_result.returncode != 0:
                print(
                    "Error: could not clear rebase state automatically.\n"
                    f"  rebase --abort: {abort.stderr.strip()}\n"
                    f"  rebase --quit:  {quit_result.stderr.strip()}\n"
                    "  Manual fix: rm -rf .git/rebase-merge .git/rebase-apply",
                    file=sys.stderr,
                )
                sys.exit(1)

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

    # Auto-commit if there are uncommitted changes
    if git_ops.has_uncommitted_changes(vault_path):
        git_ops.stage_scope_files(vault_path, config.scope)

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
    if hub.exists():
        # The pre-check above guarded against existing non-empty; an empty
        # dir is OK to remove before rename.
        hub.rmdir()
    os.rename(staging, hub)

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
            "  pip install -e <schist>/cli",
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
    """Construct a minimal valid vault.yaml data dict for the seed commit.

    Each participant gets a default scope of `research/<name>` and a matching
    write grant, plus read:* so any participant can see the full graph.
    """
    participant_entries = []
    access = {}
    for p in participants:
        scope = f"research/{p}"
        participant_entries.append({
            "name": p,
            "type": "spoke",
            "default_scope": scope,
        })
        access[p] = {"read": ["*"], "write": [scope]}

    return {
        "vault_version": 1,
        "name": name,
        "scope_convention": "subdirectory",
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
    if target.exists():
        target.rmdir()
    os.rename(staging, target)

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

    hooks_dir = staging / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    post = hooks_dir / "post-commit"
    post.write_text(POST_COMMIT_HOOK)
    post.chmod(0o755)
    pre = hooks_dir / "pre-commit"
    pre.write_text(PRE_COMMIT_HOOK)
    pre.chmod(0o755)


def _build_standalone_vault(name: str, identity: str) -> dict:
    """Construct a minimal valid vault.yaml data dict for a standalone vault.

    Single-participant, single-agent, full-vault read+write. Kept separate from
    `_build_seed_vault` because the two diverge on participant type, default
    scope, and ACL shape — parametrizing would hide the difference behind
    callbacks.
    """
    return {
        "vault_version": 1,
        "name": name,
        "scope_convention": "subdirectory",
        "participants": [{
            "name": identity,
            "type": "agent",
            "default_scope": "global",
        }],
        "access": {identity: {"read": ["*"], "write": ["*"]}},
    }


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
    hub_path = getattr(args, "hub_path", None)
    spoke = getattr(args, "spoke", False)
    hub_url = getattr(args, "hub", None)
    standalone_path = getattr(args, "path", None)

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


def _rebuild_index(vault_path: str, db_path: str) -> None:
    """Delete existing SQLite and re-ingest from markdown files.

    Uses rename-on-success: old DB is only removed after ingest succeeds.
    Before dropping the backup, the `concept_aliases` side table is copied
    forward into the new DB — it lives alongside docs/concepts/edges in
    schist.db but is not rebuilt from markdown, so on the commit-path
    rebuild it survives naturally (ingest.py runs against the existing DB).
    Here we rename the whole file, so the copy is the only thing keeping
    its rows alive.

    The `domains` table is NOT preserved here. Its source of truth is
    `vault.yaml`, and ingest.py rebuilds it from that file on every ingest
    (see `_populate_domains` in `ingestion/ingest.py`).
    """
    db = Path(db_path)
    backup = None
    if db.exists():
        backup = db.with_suffix(".db.bak")
        db.rename(backup)

    from .sqlite_query import _run_ingest

    try:
        _run_ingest(vault_path, db_path)
        if backup and backup.exists():
            _preserve_side_tables(backup, db)
            backup.unlink()
    except Exception as e:
        # Restore backup so user keeps old (stale) index rather than none
        if backup and backup.exists():
            backup.rename(db)
        print(f"Warning: index rebuild failed: {e}", file=sys.stderr)


# Side tables that live in schist.db but are not populated by ingest.py.
# Columns are hardcoded (rather than using `SELECT *`) so a schema evolution
# in `ingestion/schema.sql` cannot silently misalign columns during the copy.
# If you add a column to any listed table, update this map too — `test_sync.py`
# has a guard test that asserts every table column is listed here.
#
# `domains` is intentionally NOT here — it's rebuilt by ingest.py from
# vault.yaml on every rebuild, so preserving rows from the backup would
# resurrect entries the user just removed from vault.yaml.
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
        conn.commit()
    finally:
        conn.close()
