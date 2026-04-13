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
    for rebase_dir in (".git/rebase-merge", ".git/rebase-apply"):
        if (Path(vault_path) / rebase_dir).exists():
            print(f"Aborting leftover rebase ({rebase_dir})...", file=sys.stderr)
            subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=vault_path, capture_output=True, text=True,
            )
            break

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
    """
    name = getattr(args, "name", None)
    participants = list(getattr(args, "participant", None) or [])

    if not name:
        print("Error: --name is required for hub init", file=sys.stderr)
        sys.exit(1)
    if not participants:
        print("Error: at least one --participant is required", file=sys.stderr)
        sys.exit(1)

    hub = Path(hub_path)
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

    # Create bare repo with main as the initial branch.
    hub.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(hub)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Error: git init --bare failed: {result.stderr.strip()}", file=sys.stderr)
        shutil.rmtree(hub, ignore_errors=True)
        sys.exit(1)

    # Install pre-receive hook. Warn if the schist package isn't importable
    # by the python3 on this host — the hook will fail at push time otherwise.
    hook_path = hub / "hooks" / "pre-receive"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(PRE_RECEIVE_HOOK)
    hook_path.chmod(0o755)

    check = subprocess.run(
        ["python3", "-c", "import schist.pre_receive"],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        print(
            "Warning: `python3 -c 'import schist.pre_receive'` failed on this host.\n"
            "  The pre-receive hook will reject all pushes until the schist\n"
            "  package is installed: pip install -e <schist>/cli",
            file=sys.stderr,
        )

    # Seed the initial commit via a temp worktree — bare repos can't hold files.
    with tempfile.TemporaryDirectory(prefix="schist-hub-seed-") as tmp:
        seed = Path(tmp) / "seed"
        seed.mkdir()
        env = os.environ.copy()
        # Force identity for the seed commit so init_hub works on fresh hosts
        # without pre-configured git user.name/user.email.
        env.setdefault("GIT_AUTHOR_NAME", "schist")
        env.setdefault("GIT_AUTHOR_EMAIL", "schist@local")
        env.setdefault("GIT_COMMITTER_NAME", "schist")
        env.setdefault("GIT_COMMITTER_EMAIL", "schist@local")
        # Pre-receive identity check runs before ACL load — on the fresh bare
        # repo the ACL returns None (HEAD doesn't exist yet) and the push is
        # allowed, but only if identity is set. Use the first participant.
        env["SCHIST_IDENTITY"] = participants[0]

        def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
            return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)

        for cmd in (
            ["git", "init", "--initial-branch=main"],
            ["git", "remote", "add", "origin", str(hub)],
        ):
            r = run(cmd, seed)
            if r.returncode != 0:
                print(f"Error: {' '.join(cmd)} failed: {r.stderr.strip()}", file=sys.stderr)
                shutil.rmtree(hub, ignore_errors=True)
                sys.exit(1)

        (seed / "vault.yaml").write_text(yaml.dump(vault_data, default_flow_style=False, sort_keys=False))

        # On the seed push, the pre-receive hook short-circuits to "allow":
        # HEAD doesn't exist yet on the fresh bare, so load_acl() returns None
        # and main() returns 0. Subsequent pushes see the committed vault.yaml.
        for cmd in (
            ["git", "add", "vault.yaml"],
            ["git", "commit", "-m", f"init: seed vault {name}"],
            ["git", "push", "-u", "origin", "main"],
        ):
            r = run(cmd, seed)
            if r.returncode != 0:
                print(f"Error: {' '.join(cmd)} failed: {r.stderr.strip()}", file=sys.stderr)
                shutil.rmtree(hub, ignore_errors=True)
                sys.exit(1)

    print(f"Hub initialized at {hub_path}")
    print(f"  participants: {', '.join(participants)}")
    print()
    print("Next steps:")
    print(f"  1. Edit {hub_path}/hooks/pre-receive's environment if needed")
    print(f"  2. On each spoke, run: schist init --spoke --hub <url> --scope <scope> --identity <name>")
    print(f"  3. Review and extend vault.yaml by cloning, editing, and pushing.")


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
