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
