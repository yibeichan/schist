"""Tests for schist.git_ops.

Focused on the subprocess-timeout hardening (#256): commit() must not hang
forever when the synchronous post-commit ingest hook stalls, and a timeout
must surface as a clean (False, message) tuple rather than an exception.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from schist import git_ops


def _fake_run_timeout_on_commit(*args, **kwargs):
    """Let `git add` succeed; raise TimeoutExpired on `git commit`."""
    argv = args[0]
    if len(argv) >= 2 and argv[1] == "commit":
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_commit_passes_timeout_to_subprocess():
    """Both git add and git commit must be invoked with a timeout kwarg."""
    calls = []

    def _record(*args, **kwargs):
        calls.append((args[0], kwargs.get("timeout")))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    with patch("schist.git_ops.subprocess.run", side_effect=_record):
        ok, _ = git_ops.commit("/tmp/vault", "msg")

    assert ok is True
    # Every git invocation carried a positive timeout — none unbounded.
    assert calls, "expected git subprocess calls"
    for argv, timeout in calls:
        assert timeout is not None and timeout > 0, f"{argv} ran with no timeout"


def test_commit_returns_false_on_timeout_without_raising():
    """A stalled commit returns (False, human message), never propagates."""
    with patch("schist.git_ops.subprocess.run", side_effect=_fake_run_timeout_on_commit):
        ok, msg = git_ops.commit("/tmp/vault", "msg")

    assert ok is False
    assert "timed out" in msg


def _timeout(argv, kwargs):
    raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))


def test_sync_path_git_calls_all_carry_timeouts(tmp_path):
    """#314: every git call in the spoke-sync hot path must be bounded."""
    calls = []

    def _record(*args, **kwargs):
        calls.append((args[0], kwargs.get("timeout")))
        return subprocess.CompletedProcess(args[0], 0, stdout="main\n", stderr="")

    with patch("schist.git_ops.subprocess.run", side_effect=_record):
        git_ops.current_branch(str(tmp_path))
        git_ops.has_uncommitted_changes(str(tmp_path))
        git_ops.has_unpushed_commits(str(tmp_path))
        git_ops.stage_scope_files(str(tmp_path), "research")
        git_ops._global_scope_targets(str(tmp_path))

    assert calls, "expected git subprocess calls"
    for argv, timeout in calls:
        assert timeout is not None and timeout > 0, f"{argv} ran with no timeout"


def test_current_branch_returns_empty_on_timeout():
    """'' flows into has_unpushed_commits' detached-HEAD conservative path."""
    with patch("schist.git_ops.subprocess.run",
               side_effect=lambda *a, **k: _timeout(a[0], k)):
        assert git_ops.current_branch("/tmp/vault") == ""


def test_pull_rebase_fails_fast_when_branch_unknown():
    """#325: '' as refspec makes `git pull --rebase origin ''` silently rebase
    onto the remote's default-branch HEAD (exit 0) — it must never reach git."""
    calls = []

    def _branch_stalls_rest_records(*args, **kwargs):
        argv = args[0]
        if argv[1] == "branch":
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with patch("schist.git_ops.subprocess.run", side_effect=_branch_stalls_rest_records):
        ok, msg = git_ops.pull_rebase("/tmp/vault")

    assert ok is False
    assert "Could not determine current branch" in msg
    assert not any("pull" in argv for argv in calls), "git pull must not run with an empty refspec"


def test_push_fails_fast_when_branch_unknown():
    """#325: '' as refspec is 'fatal: invalid refspec' (or a silent no-op on
    older gits) — surface the real cause instead."""
    calls = []

    def _branch_stalls_rest_records(*args, **kwargs):
        argv = args[0]
        if argv[1] == "branch":
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with patch("schist.git_ops.subprocess.run", side_effect=_branch_stalls_rest_records):
        ok, msg = git_ops.push("/tmp/vault")

    assert ok is False
    assert "Could not determine current branch" in msg
    assert not any("push" in argv for argv in calls), "git push must not run with an empty refspec"


def test_has_uncommitted_changes_true_on_timeout():
    with patch("schist.git_ops.subprocess.run",
               side_effect=lambda *a, **k: _timeout(a[0], k)):
        assert git_ops.has_uncommitted_changes("/tmp/vault") is True


def test_has_unpushed_commits_true_on_rev_list_timeout():
    def _branch_ok_revlist_stalls(*args, **kwargs):
        argv = args[0]
        if argv[1] == "branch":
            return subprocess.CompletedProcess(argv, 0, stdout="main\n", stderr="")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    with patch("schist.git_ops.subprocess.run", side_effect=_branch_ok_revlist_stalls):
        assert git_ops.has_unpushed_commits("/tmp/vault") is True


def test_stage_scope_files_clean_message_on_timeout():
    with patch("schist.git_ops.subprocess.run",
               side_effect=lambda *a, **k: _timeout(a[0], k)):
        ok, msg = git_ops.stage_scope_files("/tmp/vault", "research")

    assert ok is False
    assert "timed out" in msg


def test_global_scope_targets_skips_stalled_dir(tmp_path):
    """A stalled ls-files skips that directory instead of hanging the sync."""
    with patch("schist.git_ops.subprocess.run",
               side_effect=lambda *a, **k: _timeout(a[0], k)):
        targets = git_ops._global_scope_targets(str(tmp_path))

    assert targets == []
