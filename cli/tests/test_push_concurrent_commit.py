"""`sync push` racing a concurrent commit on the same index.

The MCP server does not take the CLI's vault_write_lock, and both writers end
in a plain `git commit`, which commits everything staged. When the other
writer commits between `sync push` staging its scope and committing it, the
push's own commit finds nothing left and used to fail — writing a sync-error
sentinel that blocks every later MCP write, for content that was already on
the branch. These tests inject that commit at exactly that point.
"""

import subprocess
import types
from pathlib import Path

import pytest

import schist.git_ops as git_ops
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


def _with_concurrent_writer(monkeypatch, before_commit):
    """Run `before_commit(vault)` immediately before sync push's own commit."""
    real_commit = git_ops.commit

    def racing_commit(vault_path, message, files=None, *, stage=True):
        before_commit(vault_path)
        return real_commit(vault_path, message, files, stage=stage)

    monkeypatch.setattr(git_ops, "commit", racing_commit)


def _sweep_everything_staged(vault):
    # What the MCP git-writer does: a plain commit of everything staged.
    _run("git", "commit", "-qm", "feat(schist): write other note", cwd=vault)


def _push(vault):
    sync.sync_push(types.SimpleNamespace(force=False), vault, vault + "/.schist/schist.db")


def _heads(tmp_path, vault):
    local = _run("git", "rev-parse", "HEAD", cwd=vault).stdout
    hub = _run("git", "--git-dir", str(tmp_path / "hub.git"), "rev-parse", "main", cwd=tmp_path).stdout
    return local, hub


def test_push_proceeds_when_a_concurrent_commit_already_took_the_staged_files(
    tmp_path, monkeypatch, capsys
):
    vault = _spoke(tmp_path)
    (Path(vault) / "research" / "a.md").write_text("a\n")
    (Path(vault) / "research" / "b.md").write_text("b\n")
    _with_concurrent_writer(monkeypatch, _sweep_everything_staged)

    _push(vault)  # used to sys.exit(1) with "commit failed: … nothing to commit"

    local, hub = _heads(tmp_path, vault)
    assert local == hub
    tree = _run("git", "ls-tree", "-r", "--name-only", "HEAD", cwd=vault).stdout.split()
    assert {"research/a.md", "research/b.md"} <= set(tree)
    out = capsys.readouterr()
    assert "already committed by a concurrent write" in out.out
    assert "commit failed" not in out.err


def test_push_proceeds_when_the_swept_change_is_a_deletion(tmp_path, monkeypatch):
    vault = _spoke(tmp_path)
    (Path(vault) / "research" / "seed.md").unlink()
    _with_concurrent_writer(monkeypatch, _sweep_everything_staged)

    _push(vault)

    local, hub = _heads(tmp_path, vault)
    assert local == hub
    tree = _run("git", "ls-tree", "-r", "--name-only", "HEAD", cwd=vault).stdout.split()
    assert "research/seed.md" not in tree


def test_a_genuine_commit_failure_still_fails(tmp_path, monkeypatch, capsys):
    """HEAD did not move: nothing else committed our files, so the failure is
    real and must still surface (no sentinel-hiding)."""
    vault = _spoke(tmp_path)
    (Path(vault) / "research" / "a.md").write_text("a\n")
    hook = Path(_run("git", "rev-parse", "--git-path", "hooks", cwd=vault).stdout.strip())
    if not hook.is_absolute():
        hook = Path(vault) / hook
    hook.mkdir(parents=True, exist_ok=True)
    (hook / "pre-commit").write_text("#!/bin/sh\necho refused >&2\nexit 1\n")
    (hook / "pre-commit").chmod(0o755)

    with pytest.raises(SystemExit) as exc:
        _push(vault)

    assert exc.value.code == 1
    assert "commit failed" in capsys.readouterr().err


def test_head_moving_is_not_enough_when_our_change_went_to_a_stash(tmp_path, monkeypatch, capsys):
    """A concurrent pull that autostashes our staged change also moves HEAD
    and leaves the index equal to HEAD — but our content is in a stash, not
    on the branch. That must not be reported as landed."""
    vault = _spoke(tmp_path)
    (Path(vault) / "research" / "a.md").write_text("a\n")

    def stash_ours_then_commit_something_else(v):
        _run("git", "stash", "push", "-q", "-m", "concurrent-autostash-sim", "--", "research/a.md", cwd=v)
        (Path(v) / "research" / "other.md").write_text("other\n")
        _run("git", "add", "--", "research/other.md", cwd=v)
        _run("git", "commit", "-qm", "unrelated concurrent commit", cwd=v)

    _with_concurrent_writer(monkeypatch, stash_ours_then_commit_something_else)

    with pytest.raises(SystemExit) as exc:
        _push(vault)

    assert exc.value.code == 1
    assert "commit failed" in capsys.readouterr().err
    tree = _run("git", "ls-tree", "-r", "--name-only", "HEAD", cwd=vault).stdout.split()
    assert "research/a.md" not in tree


def test_landed_check_compares_blobs_not_just_paths(tmp_path):
    """Same path in HEAD with DIFFERENT content than we staged → not landed."""
    vault = _spoke(tmp_path)
    head_before = git_ops._head_sha(vault)
    (Path(vault) / "research" / "seed.md").write_text("ours\n")
    _run("git", "add", "--", "research/seed.md", cwd=vault)
    ours = git_ops.index_entries(vault, ["research/seed.md"])
    (Path(vault) / "research" / "seed.md").write_text("theirs\n")
    _run("git", "add", "--", "research/seed.md", cwd=vault)
    _run("git", "commit", "-qm", "theirs", cwd=vault)

    assert not git_ops.landed_via_concurrent_commit(vault, head_before, ours)

    (Path(vault) / "research" / "seed.md").write_text("ours\n")
    _run("git", "add", "--", "research/seed.md", cwd=vault)
    _run("git", "commit", "-qm", "ours lands", cwd=vault)
    assert git_ops.landed_via_concurrent_commit(vault, head_before, ours)
