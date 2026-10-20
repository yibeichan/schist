# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""`sync push --no-commit`: push existing commits, never touch the index (#724).

The MCP server commits every write itself, then spawns a background push. When
that push also staged and committed uncommitted scope files, its `git add` /
`git commit` took the index lock out from under the server's next write (#719),
and its commit swept that write's staged note in under a generic message. With
--no-commit the background push has nothing to do with the index at all.
"""

import subprocess
import sys
import types
from pathlib import Path

import schist.sync as sync
from schist.sync import SpokeConfig, save_spoke_config


def _run(*args, cwd):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, check=True)


def _spoke(tmp_path: Path) -> str:
    hub = tmp_path / "hub.git"
    subprocess.run(["git", "init", "-q", "--bare", str(hub)], check=True)
    vault = tmp_path / "vault"
    subprocess.run(["git", "clone", "-q", str(hub), str(vault)], check=True, capture_output=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        _run("git", "config", k, v, cwd=vault)
    (vault / "research").mkdir()
    (vault / "research" / "seed.md").write_text("seed\n")
    _run("git", "add", ".", cwd=vault)
    _run("git", "commit", "-qm", "seed", cwd=vault)
    _run("git", "branch", "-M", "main", cwd=vault)
    _run("git", "push", "-q", "-u", "origin", "main", cwd=vault)
    save_spoke_config(str(vault), SpokeConfig(hub=str(hub), identity="t", scope="research"))
    return str(vault)


def _commit_note(vault: str, name: str) -> None:
    (Path(vault) / "research" / name).write_text(f"{name}\n")
    _run("git", "add", "--", f"research/{name}", cwd=vault)
    _run("git", "commit", "-qm", f"feat(schist): write {name} — by t", cwd=vault)


def _hub_head(tmp_path: Path) -> str:
    return _run("git", "--git-dir", str(tmp_path / "hub.git"), "rev-parse", "main", cwd=tmp_path).stdout


def _local_head(vault: str) -> str:
    return _run("git", "rev-parse", "HEAD", cwd=vault).stdout


def _push_no_commit(vault: str) -> None:
    sync.sync_push(types.SimpleNamespace(no_commit=True), vault, vault + "/.schist/schist.db")


def test_pushes_existing_commits_and_leaves_the_index_and_worktree_alone(tmp_path, capsys):
    vault = _spoke(tmp_path)
    _commit_note(vault, "committed.md")
    # A write staged but not yet committed (what the MCP server has between
    # its `git add` and its `git commit`), and a stray hand edit.
    (Path(vault) / "research" / "staged.md").write_text("staged\n")
    _run("git", "add", "--", "research/staged.md", cwd=vault)
    (Path(vault) / "research" / "stray.md").write_text("stray\n")
    head_before = _local_head(vault)

    _push_no_commit(vault)

    assert _local_head(vault) == head_before, "no commit of its own"
    assert _hub_head(tmp_path) == head_before, "the existing commit reached the hub"
    status = _run("git", "status", "--porcelain", cwd=vault).stdout
    assert "A  research/staged.md" in status, status  # still staged, not swept
    assert "?? research/stray.md" in status, status   # still uncommitted
    assert "uncommitted" in capsys.readouterr().out


def test_a_live_index_lock_does_not_stop_the_push(tmp_path):
    """The lock is the MCP server's own commit in progress. A push that never
    touches the index has no reason to refuse it, or to remove it."""
    vault = _spoke(tmp_path)
    _commit_note(vault, "committed.md")
    lock = Path(vault) / ".git" / "index.lock"
    lock.write_text("")

    _push_no_commit(vault)

    assert _hub_head(tmp_path) == _local_head(vault)
    assert lock.exists()


def test_nothing_to_push_is_still_a_clean_exit(tmp_path, capsys):
    vault = _spoke(tmp_path)
    (Path(vault) / "research" / "stray.md").write_text("stray\n")

    _push_no_commit(vault)

    out = capsys.readouterr().out
    assert "Nothing to push" in out
    assert "?? research/stray.md" in _run("git", "status", "--porcelain", cwd=vault).stdout


def test_the_cli_flag_reaches_sync_push(tmp_path):
    vault = _spoke(tmp_path)
    _commit_note(vault, "committed.md")
    (Path(vault) / "research" / "stray.md").write_text("stray\n")

    r = subprocess.run(
        [sys.executable, "-m", "schist", "--vault", vault, "sync", "push", "--no-commit"],
        capture_output=True, text=True,
    )

    assert r.returncode == 0, r.stderr
    assert _hub_head(tmp_path) == _local_head(vault)
    assert "?? research/stray.md" in _run("git", "status", "--porcelain", cwd=vault).stdout


def test_the_uncommitted_check_takes_no_optional_index_lock(monkeypatch):
    """`git status` writes a refreshed index back under index.lock unless told
    not to, which collides with a concurrent MCP commit."""
    import schist.git_ops as git_ops
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)
    git_ops.has_uncommitted_changes("/nonexistent")
    assert seen and seen[0][:3] == ["git", "--no-optional-locks", "status"], seen


# --commit-only: the other half of the background push. The MCP server runs it
# under its own write lock when the vault has uncommitted files (hand edits),
# so they keep syncing without racing the server's writes, then pushes with
# --no-commit outside the lock.

def _push_commit_only(vault: str) -> None:
    sync.sync_push(types.SimpleNamespace(commit_only=True), vault, vault + "/.schist/schist.db")


def test_commit_only_commits_in_scope_changes_and_does_not_push(tmp_path, capsys):
    vault = _spoke(tmp_path)
    hub_before = _hub_head(tmp_path)
    (Path(vault) / "research" / "hand-edit.md").write_text("edited in an editor\n")

    _push_commit_only(vault)

    assert _local_head(vault) != hub_before, "a sync commit was made"
    assert _hub_head(tmp_path) == hub_before, "nothing was pushed"
    assert _run("git", "status", "--porcelain", "--", "research", cwd=vault).stdout == ""
    subject = _run("git", "log", "-1", "--format=%s", cwd=vault).stdout.strip()
    assert subject == "sync(t): 1 file"
    assert "Pushing" not in capsys.readouterr().out


def test_commit_only_with_nothing_to_commit_is_a_clean_no_op(tmp_path):
    vault = _spoke(tmp_path)
    head = _local_head(vault)

    _push_commit_only(vault)

    assert _local_head(vault) == head


def test_commit_only_and_no_commit_are_mutually_exclusive(tmp_path):
    vault = _spoke(tmp_path)
    r = subprocess.run(
        [sys.executable, "-m", "schist", "--vault", vault, "sync", "push", "--commit-only", "--no-commit"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "not allowed with" in r.stderr
