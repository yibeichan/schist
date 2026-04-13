"""Spoke sync operations: init, pull, push. Also hub init."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from . import git_ops
from .acl import ACLError, parse_vault_data
from .spoke_config import SpokeConfig, is_spoke, load_spoke_config, save_spoke_config

class _HubInitError(Exception):
    """Raised by init_hub build steps so the outer function can clean up staging."""


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
        print(f"Error: pull failed — {output}", file=sys.stderr)
        if "conflict" in output.lower() or "CONFLICT" in output:
            print("Resolve conflicts manually or re-clone the spoke.", file=sys.stderr)
        sys.exit(1)

    # Rebuild index
    _rebuild_index(vault_path, db_path)
    print(f"Pull complete. Index rebuilt.")


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
    except _HubInitError as e:
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

    Raises _HubInitError with a descriptive message on any failure. The caller
    is responsible for cleaning up `staging`.
    """
    # 1. Create bare repo at the staging path
    staging.mkdir(parents=True)
    result = subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(staging)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise _HubInitError(f"git init --bare failed: {result.stderr.strip()}")

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
                raise _HubInitError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")

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
                raise _HubInitError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")


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


def _rebuild_index(vault_path: str, db_path: str) -> None:
    """Delete existing SQLite and re-ingest from markdown files.

    Uses rename-on-success: old DB is only removed after ingest succeeds.
    """
    db = Path(db_path)
    backup = None
    if db.exists():
        backup = db.with_suffix(".db.bak")
        db.rename(backup)

    from .sqlite_query import _run_ingest

    try:
        _run_ingest(vault_path, db_path)
        # Success — remove backup
        if backup and backup.exists():
            backup.unlink()
    except Exception as e:
        # Restore backup so user keeps old (stale) index rather than none
        if backup and backup.exists():
            backup.rename(db)
        print(f"Warning: index rebuild failed: {e}", file=sys.stderr)
