"""Tests for schist.git_ops.

Focused on the subprocess-timeout hardening (#256) and its #364 refinement:
commit() must not hang forever when the synchronous post-commit ingest hook
stalls, a timeout must surface as a clean tuple rather than an exception —
and a hook stall AFTER the branch ref moved must report (True, warning),
never a false failure, with the whole hook process group killed rather than
orphaned (mirrors git-writer.ts #336/#355).
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from unittest.mock import patch

import pytest

from schist import git_ops


class _FakeProc:
    """Stand-in for the Popen'd `git commit`: succeed, stall, or stall-once."""

    def __init__(self, timeout_on_first: bool = False, record: list | None = None):
        self.pid = 424242
        self.returncode = 0
        self._timeout_on_first = timeout_on_first
        self._calls = 0
        self._record = record if record is not None else []

    def communicate(self, timeout=None):
        self._calls += 1
        self._record.append(timeout)
        if self._timeout_on_first and self._calls == 1:
            raise subprocess.TimeoutExpired(cmd=["git", "commit"], timeout=timeout)
        return "", ""


def test_commit_passes_timeout_to_subprocess():
    """git add / rev-parse (run) and git commit (communicate) are all bounded."""
    run_calls = []
    communicate_timeouts: list = []

    def _record(*args, **kwargs):
        run_calls.append((args[0], kwargs.get("timeout")))
        return subprocess.CompletedProcess(args[0], 0, stdout="abc123\n", stderr="")

    with patch("schist.git_ops.subprocess.run", side_effect=_record), \
         patch("schist.git_ops.subprocess.Popen",
               return_value=_FakeProc(record=communicate_timeouts)):
        ok, _ = git_ops.commit("/tmp/vault", "msg")

    assert ok is True
    # Every git invocation carried a positive timeout — none unbounded.
    assert run_calls, "expected git subprocess calls"
    for argv, timeout in run_calls:
        assert timeout is not None and timeout > 0, f"{argv} ran with no timeout"
    assert communicate_timeouts == [git_ops.COMMIT_TIMEOUT]


def test_commit_returns_false_on_timeout_without_raising():
    """A stalled commit whose ref did NOT move returns (False, message), never
    propagates — and SIGKILLs the whole process group, not just git."""
    killed = []

    def _same_head(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="samesha\n", stderr="")

    with patch("schist.git_ops.subprocess.run", side_effect=_same_head), \
         patch("schist.git_ops.subprocess.Popen",
               return_value=_FakeProc(timeout_on_first=True)), \
         patch("schist.git_ops.os.killpg",
               side_effect=lambda pgid, sig: killed.append((pgid, sig))):
        ok, msg = git_ops.commit("/tmp/vault", "msg")

    assert ok is False
    assert "timed out" in msg
    assert killed == [(424242, signal.SIGKILL)]


def test_commit_true_when_ref_moved_but_hook_stalled():
    """#364: git updates the branch ref BEFORE the post-commit hook runs, so a
    hook stall fires the timeout on a commit that already landed. Returning
    False made `schist add`/`link` report an error — and sync push retry —
    for a write that succeeded."""
    heads = iter(["oldsha\n", "newsha\n"])

    def _run(*args, **kwargs):
        argv = args[0]
        if argv[1] == "rev-parse":
            return subprocess.CompletedProcess(argv, 0, stdout=next(heads), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with patch("schist.git_ops.subprocess.run", side_effect=_run), \
         patch("schist.git_ops.subprocess.Popen",
               return_value=_FakeProc(timeout_on_first=True)), \
         patch("schist.git_ops.os.killpg", side_effect=lambda pgid, sig: None):
        ok, msg = git_ops.commit("/tmp/vault", "msg")

    assert ok is True
    assert "index may lag" in msg
    # Callers (commands.py add/link, sync.py push) surface the warning by
    # matching this prefix — the message must keep starting with it.
    assert msg.startswith(git_ops.HOOK_STALL_WARNING_PREFIX)


# ---------------------------------------------------------------------------
# commit() against a real repo with real hooks (#364 end-to-end)
# ---------------------------------------------------------------------------


def _init_repo(path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    for k, v in (("user.email", "test@test"), ("user.name", "test")):
        subprocess.run(["git", "-C", str(path), "config", k, v],
                       check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "--allow-empty",
                    "-m", "seed", "--no-verify"], check=True, capture_output=True)


def _head(path) -> str:
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


def _write_hook(path, name: str, body: str) -> None:
    hook = path / ".git" / "hooks" / name
    hook.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    hook.chmod(0o755)


def test_commit_real_stalled_post_commit_hook_reports_success_and_kills_chain(
    tmp_path, monkeypatch
) -> None:
    """The empirical #364 repro as a regression test: a post-commit hook that
    outlives the timeout must yield (True, warning) because the commit landed,
    return promptly (the old run() reap blocked on the pipe the orphan held
    open for the hook's FULL runtime), and leave no orphaned hook process."""
    _init_repo(tmp_path)
    _write_hook(tmp_path, "post-commit", "echo $$ > hookpid\nsleep 8\n")
    (tmp_path / "note.md").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(git_ops, "COMMIT_TIMEOUT", 1)

    pre = _head(tmp_path)
    t0 = time.monotonic()
    ok, msg = git_ops.commit(str(tmp_path), "test commit")
    elapsed = time.monotonic() - t0

    assert ok is True
    assert "index may lag" in msg
    assert _head(tmp_path) != pre, "commit should have landed before the stall"
    assert elapsed < 6, f"reap blocked on the orphan's pipe for {elapsed:.1f}s"

    # The hook chain was killed with the group — poll briefly for the SIGKILL
    # to land and the reparented sh to be reaped.
    hook_pid = int((tmp_path / "hookpid").read_text().strip())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(hook_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(hook_pid, signal.SIGKILL)  # don't leak it past the test
        raise AssertionError("stalled hook survived commit()'s group kill")


def test_commit_real_stalled_pre_commit_hook_is_a_true_failure(
    tmp_path, monkeypatch
) -> None:
    """A stall BEFORE the ref moves (pre-commit) is a genuine failure: HEAD
    unchanged, so the re-check must NOT flip it to success."""
    _init_repo(tmp_path)
    _write_hook(tmp_path, "pre-commit", "sleep 8\n")
    (tmp_path / "note.md").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(git_ops, "COMMIT_TIMEOUT", 1)

    pre = _head(tmp_path)
    ok, msg = git_ops.commit(str(tmp_path), "test commit")

    assert ok is False
    assert "timed out" in msg
    assert _head(tmp_path) == pre


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


# ---------------------------------------------------------------------------
# setup_sparse_checkout (#345)
# ---------------------------------------------------------------------------


def test_setup_sparse_checkout_calls_all_carry_timeouts():
    """#345: init --cone, set <scope>, and checkout must all be bounded —
    each can hang forever on a stale index.lock or NFS stall."""
    calls = []

    def _record(*args, **kwargs):
        calls.append((args[0], kwargs.get("timeout")))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    with patch("schist.git_ops.subprocess.run", side_effect=_record):
        ok, _ = git_ops.setup_sparse_checkout("/tmp/vault", "research")

    assert ok is True
    assert len(calls) == 3, "expected sparse-checkout init/set + checkout"
    for argv, timeout in calls:
        assert timeout is not None and timeout > 0, f"{argv} ran with no timeout"
    # #354 review: the config ops (init/set) are lock-shaped → 30s; the final
    # `git checkout` materializes the whole scope worktree (bulk I/O) → the
    # 120s clone/commit ceiling, so a large NFS/Lustre vault doesn't fail it.
    by_op = {argv[1]: timeout for argv, timeout in calls}
    assert by_op["sparse-checkout"] == 30
    assert by_op["checkout"] == 120


def test_setup_sparse_checkout_returns_false_on_timeout():
    """A stalled first call surfaces as (False, message), never an exception."""
    with patch("schist.git_ops.subprocess.run",
               side_effect=lambda *a, **k: _timeout(a[0], k)):
        ok, msg = git_ops.setup_sparse_checkout("/tmp/vault", "research")

    assert ok is False
    assert "timed out" in msg


def test_setup_sparse_checkout_timeout_mid_sequence():
    """Timeout on the final `git checkout` (after sparse-checkout succeeded)
    takes the same clean (False, message) path."""
    def _checkout_stalls(*args, **kwargs):
        argv = args[0]
        if argv[1] == "checkout":
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with patch("schist.git_ops.subprocess.run", side_effect=_checkout_stalls):
        ok, msg = git_ops.setup_sparse_checkout("/tmp/vault", "research")

    assert ok is False
    assert "timed out" in msg


# ---------------------------------------------------------------------------
# pull_rebase cleanup-abort bounds (#321)
# ---------------------------------------------------------------------------


def test_pull_rebase_abort_bounded_and_abort_timeout_keeps_pull_error():
    """#321: the best-effort `rebase --abort` after a failed pull carries a
    timeout, and its own stall must not mask the original pull failure."""
    abort_calls = []

    def _pull_fails_abort_stalls(*args, **kwargs):
        argv = args[0]
        if argv[1] == "branch":
            return subprocess.CompletedProcess(argv, 0, stdout="main\n", stderr="")
        if argv[1] == "pull":
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="CONFLICT (content): Merge conflict in a.md"
            )
        abort_calls.append((argv, kwargs.get("timeout")))
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    with patch("schist.git_ops.subprocess.run", side_effect=_pull_fails_abort_stalls):
        ok, msg = git_ops.pull_rebase("/tmp/vault")

    assert ok is False
    assert "CONFLICT" in msg
    assert abort_calls, "expected a rebase --abort attempt"
    argv, timeout = abort_calls[0]
    assert argv[1:3] == ["rebase", "--abort"]
    assert timeout is not None and timeout > 0


def test_pull_rebase_timeout_then_abort_timeout_does_not_raise():
    """Pull stalls AND the cleanup abort stalls — still a clean (False, msg)."""
    def _all_stall_except_branch(*args, **kwargs):
        argv = args[0]
        if argv[1] == "branch":
            return subprocess.CompletedProcess(argv, 0, stdout="main\n", stderr="")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    with patch("schist.git_ops.subprocess.run", side_effect=_all_stall_except_branch):
        ok, msg = git_ops.pull_rebase("/tmp/vault")

    assert ok is False
    assert "timed out" in msg


# ---------------------------------------------------------------------------
# .gitignore vs scope content (#361)
# ---------------------------------------------------------------------------


def _commit_all(path, msg: str) -> None:
    subprocess.run(["git", "-C", str(path), "add", "-A", "-f"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", msg,
                    "--no-verify"], check=True, capture_output=True)


def test_stage_scope_files_fails_loudly_on_ignored_scope_files(tmp_path) -> None:
    """#361: `git add -- research/` skips .gitignore-matched notes with exit 0
    and no output — the guard must turn that silent drop into a hard error
    that names the files."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("research/secret*.md\n", encoding="utf-8")
    _commit_all(tmp_path, "hub gitignore")

    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "secret-plan.md").write_text("hidden\n", encoding="utf-8")
    (tmp_path / "research" / "normal.md").write_text("visible\n", encoding="utf-8")

    ok, msg = git_ops.stage_scope_files(str(tmp_path), "research")

    assert ok is False
    assert "research/secret-plan.md" in msg
    assert "never reach the hub" in msg
    # The visible note still staged — nothing is lost once the operator
    # fixes the .gitignore and the next push picks everything up.
    staged = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "research/normal.md" in staged


def test_stage_scope_files_ok_when_ignores_do_not_touch_scope(tmp_path) -> None:
    """The expected vault .gitignore (`.schist/`, #354) matches nothing under
    a content scope — the guard must not fire on it."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".schist/\n", encoding="utf-8")
    _commit_all(tmp_path, "hub gitignore")

    (tmp_path / ".schist").mkdir()
    (tmp_path / ".schist" / "schist.db").write_text("x", encoding="utf-8")
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "note.md").write_text("ok\n", encoding="utf-8")

    ok, msg = git_ops.stage_scope_files(str(tmp_path), "research")
    assert ok is True, msg


def test_ignored_scope_files_detects_ignored_only_change(tmp_path) -> None:
    """#361 permanent-drop corner: when the ignored note is the ONLY change,
    `git status --porcelain` is empty (has_uncommitted_changes -> False), so
    sync_push consults this guard directly to avoid no-opping forever."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("research/secret*.md\n", encoding="utf-8")
    _commit_all(tmp_path, "hub gitignore")

    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "secret-only.md").write_text("hidden\n", encoding="utf-8")

    assert git_ops.has_uncommitted_changes(str(tmp_path)) is False
    assert git_ops.ignored_scope_files(str(tmp_path), "research") == [
        "research/secret-only.md"
    ]


def test_ignored_scope_files_empty_on_probe_timeout() -> None:
    """Availability over strictness: a stalled probe must not block sync."""
    with patch("schist.git_ops.subprocess.run",
               side_effect=lambda *a, **k: _timeout(a[0], k)):
        assert git_ops.ignored_scope_files("/tmp/vault", "research") == []


# ---------------------------------------------------------------------------
# push() vs stalled pre-receive hook chain (#379)
# ---------------------------------------------------------------------------


def test_push_real_stalled_pre_receive_hook_kills_chain(
    tmp_path, monkeypatch
) -> None:
    """The empirical #379 repro as a regression test: a pre-receive hook that
    outlives PUSH_TIMEOUT must not leave git-receive-pack + the hook chain
    orphaned — plain run(timeout=) killed only the push client."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)],
                   check=True, capture_output=True)
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\necho $$ > {tmp_path}/hookpid\nsleep 60\n",
                    encoding="utf-8")
    hook.chmod(0o755)

    vault = tmp_path / "vault"
    vault.mkdir()
    _init_repo(vault)
    subprocess.run(["git", "-C", str(vault), "remote", "add", "origin",
                    str(origin)], check=True, capture_output=True)
    (vault / "note.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(vault), "add", "note.md"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(vault), "commit", "-q", "-m", "n",
                    "--no-verify"], check=True, capture_output=True)
    monkeypatch.setattr(git_ops, "PUSH_TIMEOUT", 1)

    t0 = time.monotonic()
    ok, msg = git_ops.push(str(vault))
    elapsed = time.monotonic() - t0

    assert ok is False
    assert "timed out after 1s" in msg
    assert elapsed < 8, f"push reap blocked for {elapsed:.1f}s"

    hook_pid = int((tmp_path / "hookpid").read_text().strip())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(hook_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(hook_pid, signal.SIGKILL)  # don't leak it past the test
        raise AssertionError("stalled pre-receive hook survived push()'s group kill")


def test_run_group_killable_takes_grandchildren_with_the_group(tmp_path) -> None:
    """The shared helper's contract: on timeout the WHOLE process group dies,
    not just the direct child — the seed-push path (#379) relies on this via
    sync._build_hub_in_staging's run() closure."""
    pidfile = tmp_path / "grandchild"
    with pytest.raises(subprocess.TimeoutExpired):
        git_ops.run_group_killable(
            ["/bin/sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"],
            cwd=str(tmp_path), timeout=1,
        )
    grandchild = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(grandchild, signal.SIGKILL)
        raise AssertionError("grandchild survived the group kill")
