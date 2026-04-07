"""Spoke sync operations: init, pull, push."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import git_ops
from .spoke_config import SpokeConfig, is_spoke, load_spoke_config, save_spoke_config


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
            ok, output = git_ops.commit(vault_path, msg)
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


def _rebuild_index(vault_path: str, db_path: str) -> None:
    """Delete existing SQLite and re-ingest from markdown files."""
    db = Path(db_path)
    if db.exists():
        db.unlink()

    from .sqlite_query import _run_ingest

    try:
        _run_ingest(vault_path, db_path)
    except Exception as e:
        print(f"Warning: index rebuild failed: {e}", file=sys.stderr)
