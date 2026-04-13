"""End-to-end hub+spoke smoke test using a local file:// bare hub.

Creates a bare hub with init_hub, clones two spokes, writes a note on one,
syncs, and asserts the note appears on the other.

Marked as integration (slow — real git subprocesses). Run with:
    python -m pytest cli/tests/test_hub_spoke_e2e.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def _has_git() -> bool:
    return shutil.which("git") is not None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _has_git(), reason="git not available"),
]


def _run(cmd, cwd=None, env=None, check=True):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=check)


def _init_spoke_via_cli(spoke_path: Path, hub_url: str, scope: str, identity: str):
    """Clone hub into spoke_path via schist CLI, returning the vault dir."""
    env = os.environ.copy()
    env["SCHIST_IDENTITY"] = identity
    _run(
        [
            "python3", "-m", "schist",
            "--vault", str(spoke_path),
            "init", "--spoke",
            "--hub", hub_url,
            "--scope", scope,
            "--identity", identity,
        ],
        env=env,
    )


def test_hub_spoke_roundtrip(tmp_path):
    from schist.sync import init_hub

    # 1. Create hub
    hub = tmp_path / "hub.git"
    init_hub(
        SimpleNamespace(name="e2e-test", participant=["alpha", "beta"]),
        str(hub),
    )
    hub_url = f"file://{hub}"

    # 2. Two spokes
    spoke_a = tmp_path / "spoke-a"
    spoke_b = tmp_path / "spoke-b"
    _init_spoke_via_cli(spoke_a, hub_url, "research/alpha", "alpha")
    _init_spoke_via_cli(spoke_b, hub_url, "research/beta", "beta")

    # 3. Alpha writes a note inside its scope
    note_rel = "research/alpha/2026-04-12-from-alpha.md"
    note_path = spoke_a / note_rel
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        "---\ntitle: from alpha\ndate: 2026-04-12\nstatus: draft\n---\n\nhi beta\n"
    )

    # Use git to commit directly (simulating what MCP would do)
    _run(["git", "config", "user.email", "a@test"], cwd=spoke_a)
    _run(["git", "config", "user.name", "a"], cwd=spoke_a)
    _run(["git", "add", note_rel], cwd=spoke_a)
    _run(["git", "commit", "-m", "alpha: add note"], cwd=spoke_a)

    # 4. Alpha pushes via schist sync push
    env_a = os.environ.copy()
    env_a["SCHIST_IDENTITY"] = "alpha"
    _run(
        ["python3", "-m", "schist", "--vault", str(spoke_a), "sync", "push"],
        env=env_a,
    )

    # 5. Beta pulls via schist sync pull — but beta's sparse checkout is
    #    research/beta, so it won't have research/alpha files on disk. The
    #    commit is still in beta's git history though, so verify via git show.
    env_b = os.environ.copy()
    env_b["SCHIST_IDENTITY"] = "beta"
    _run(
        ["python3", "-m", "schist", "--vault", str(spoke_b), "sync", "pull"],
        env=env_b,
    )

    # Beta's working tree should NOT contain alpha's file (sparse checkout)
    assert not (spoke_b / note_rel).exists()

    # But the commit is in beta's history — verify via git log
    result = _run(
        ["git", "log", "--all", "--oneline"],
        cwd=spoke_b,
    )
    assert "alpha: add note" in result.stdout

    # And the file content is reachable via git show
    result = _run(
        ["git", "show", f"HEAD:{note_rel}"],
        cwd=spoke_b,
    )
    assert "from alpha" in result.stdout
    assert "hi beta" in result.stdout


# Note: cross-scope push rejection is covered by
# test_hub.py::test_hub_push_then_rejects_out_of_scope (without sparse checkout,
# which would block the write at the spoke level before pre-receive even runs).
