"""`schist sync pull --autostash`: pulling over uncommitted edits.

Without --autostash, any uncommitted edit to a tracked file fails the pull
outright ("cannot pull with rebase: You have unstaged changes"). Autostash is
opt-in for explicitly requested pulls only; implicit pulls keep refusing.
These run real git against a local bare hub: the behaviour under test is
git's, and the hazards (a stash git did not re-apply still exits 0; abort and
--quit must hand the edits back) are only observable end to end.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from schist import git_ops


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{result.stdout}{result.stderr}")
    return result


def _hub_and_two_clones(tmp_path: Path) -> tuple[Path, Path]:
    """A bare hub, an `other` clone that pushes, and the `spoke` under test."""
    hub = tmp_path / "hub.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(hub))
    other, spoke = tmp_path / "other", tmp_path / "spoke"
    _git(tmp_path, "clone", "-q", str(hub), str(other))
    _git(other, "config", "user.email", "o@o")
    _git(other, "config", "user.name", "other")
    (other / "notes.md").write_text("one\ntwo\nthree\n")
    (other / "shared.md").write_text("base\n")
    (other / "aside.md").write_text("aside\n")
    _git(other, "add", ".")
    _git(other, "commit", "-qm", "seed")
    _git(other, "push", "-q", "origin", "main")
    _git(tmp_path, "clone", "-q", str(hub), str(spoke))
    _git(spoke, "config", "user.email", "s@s")
    _git(spoke, "config", "user.name", "spoke")
    return other, spoke


def _remote_commit(other: Path, name: str, text: str) -> None:
    (other / name).write_text(text)
    _git(other, "add", name)
    _git(other, "commit", "-qm", f"remote {name}")
    _git(other, "push", "-q", "origin", "main")


def _local_commit(spoke: Path, name: str, text: str) -> None:
    (spoke / name).write_text(text)
    _git(spoke, "add", name)
    _git(spoke, "commit", "-qm", f"local {name}")


def _post_rewrite_hook(spoke: Path, body: str) -> None:
    """Runs after the rebase rewrites the local commit, BEFORE git re-applies
    the autostash -- the window a concurrent writer would hit."""
    hook = spoke / ".git" / "hooks" / "post-rewrite"
    hook.write_text("#!/bin/sh\n" + body)
    hook.chmod(0o755)


def _stash_count(repo: Path) -> int:
    return len(_git(repo, "stash", "list").stdout.splitlines())


# --- opt-in: implicit pulls never stash --------------------------------------

def test_without_autostash_a_dirty_tree_still_refuses_and_nothing_is_stashed(tmp_path):
    other, spoke = _hub_and_two_clones(tmp_path)
    _remote_commit(other, "shared.md", "from the hub\n")
    (spoke / "notes.md").write_text("one\nhalf-finished edit\nthree\n")
    # A user's own rebase.autoStash must not turn an implicit pull into one.
    _git(spoke, "config", "rebase.autoStash", "true")

    ok, _output = git_ops.pull_rebase(str(spoke))

    assert not ok
    assert _stash_count(spoke) == 0
    assert (spoke / "notes.md").read_text() == "one\nhalf-finished edit\nthree\n"
    assert (spoke / "shared.md").read_text() == "base\n"  # nothing pulled


def test_sync_pull_autostashes_only_when_the_flag_is_set(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from schist import sync
    from .test_sync import _make_spoke

    vault = _make_spoke(tmp_path)
    seen = []
    monkeypatch.setattr(sync.git_ops, "pull_rebase",
                        lambda v, autostash=False: (seen.append(autostash), (True, ""))[1])
    monkeypatch.setattr(sync, "_rebuild_index", lambda v, d: None)

    sync.sync_pull(SimpleNamespace(autostash=True), vault, "db")
    sync.sync_pull(SimpleNamespace(), vault, "db")  # `schist sync --force --pull`
    sync.sync_pull(MagicMock(), vault, "db")  # an unset attribute is not True

    assert seen == [True, False, False]


# --- explicit autostash pulls --------------------------------------------------

def test_uncommitted_edit_no_longer_blocks_an_autostash_pull(tmp_path):
    other, spoke = _hub_and_two_clones(tmp_path)
    _remote_commit(other, "shared.md", "from the hub\n")
    (spoke / "notes.md").write_text("one\nhalf-finished edit\nthree\n")

    ok, output = git_ops.pull_rebase(str(spoke), autostash=True)

    assert ok, output
    assert (spoke / "shared.md").read_text() == "from the hub\n"
    assert (spoke / "notes.md").read_text() == "one\nhalf-finished edit\nthree\n"
    assert _stash_count(spoke) == 0  # re-applied, not left behind


def test_stash_that_will_not_reapply_is_saved_and_the_tree_reset(tmp_path):
    """git exits 0 here, leaving conflict markers that a push would publish."""
    other, spoke = _hub_and_two_clones(tmp_path)
    _remote_commit(other, "shared.md", "hub version\n")
    _local_commit(spoke, "notes.md", "one\ntwo\nthree\nlocal commit\n")
    (spoke / "shared.md").write_text("my uncommitted version\n")

    ok, output = git_ops.pull_rebase(str(spoke), autostash=True)

    assert not ok
    assert output.startswith(git_ops.PULL_AUTOSTASH_CONFLICT_PREFIX)
    log = _git(spoke, "log", "--format=%s", "-2").stdout.split("\n")
    assert log[:2] == ["local notes.md", "remote shared.md"]
    assert _git(spoke, "ls-files", "--unmerged").stdout == ""
    assert (spoke / "shared.md").read_text() == "hub version\n"
    assert _stash_count(spoke) == 1
    assert "my uncommitted version" in _git(spoke, "stash", "show", "-p").stdout


@pytest.mark.parametrize("hook,dirty", [
    # The stashed path is rewritten during the pull: git cannot apply over it.
    ("echo rewritten-by-hook > notes.md\n", "modified"),
    # A path the user staged as NEW now exists untracked: same outcome.
    ("echo created-by-hook > brand-new.md\n", "added"),
], ids=["rewrites-stashed-path", "creates-staged-new-path"])
def test_a_stash_git_did_not_reapply_fails_loudly_even_without_conflicts(
        tmp_path, hook, dirty):
    """git exits 0, leaves NO unmerged entries, and keeps the edit only in the
    stash. Read by unmerged entries alone this was a clean success, and the
    edit vanished from the tree without a word."""
    other, spoke = _hub_and_two_clones(tmp_path)
    _remote_commit(other, "shared.md", "hub version\n")
    _local_commit(spoke, "aside.md", "local commit\n")
    if dirty == "modified":
        (spoke / "notes.md").write_text("one\nmy edit\nthree\n")
    else:
        (spoke / "brand-new.md").write_text("my new file\n")
        _git(spoke, "add", "brand-new.md")
    _post_rewrite_hook(spoke, hook)

    ok, output = git_ops.pull_rebase(str(spoke), autostash=True)

    assert not ok, output
    assert output.startswith(git_ops.PULL_AUTOSTASH_CONFLICT_PREFIX)
    stash_sha = _git(spoke, "rev-parse", "refs/stash").stdout.strip()
    assert stash_sha[:12] in output
    assert "NOT re-applied" in output
    assert _stash_count(spoke) == 1


def test_a_conflicted_tree_that_something_else_also_changed_is_not_reset(tmp_path):
    """Unmerged entries alone do not license `reset --hard`: the hook's edit
    to a path the stash does not hold must survive."""
    other, spoke = _hub_and_two_clones(tmp_path)
    _remote_commit(other, "shared.md", "hub version\n")
    _local_commit(spoke, "notes.md", "one\ntwo\nthree\nlocal commit\n")
    (spoke / "shared.md").write_text("my uncommitted version\n")
    _post_rewrite_hook(spoke, "echo concurrent-writer > aside.md\n")

    ok, output = git_ops.pull_rebase(str(spoke), autostash=True)

    assert not ok
    assert output.startswith(git_ops.PULL_AUTOSTASH_CONFLICT_PREFIX)
    assert "NOT reset" in output and "aside.md" in output
    assert (spoke / "aside.md").read_text() == "concurrent-writer\n"
    assert _git(spoke, "ls-files", "--unmerged").stdout != ""  # left as-is
    assert _stash_count(spoke) == 1


def test_a_real_rebase_conflict_gives_the_uncommitted_edits_back(tmp_path):
    other, spoke = _hub_and_two_clones(tmp_path)
    _remote_commit(other, "shared.md", "hub version\n")
    _local_commit(spoke, "shared.md", "conflicting local commit\n")
    (spoke / "notes.md").write_text("one\nuncommitted\nthree\n")

    ok, output = git_ops.pull_rebase(str(spoke), autostash=True)

    assert not ok
    assert not output.startswith(git_ops.PULL_AUTOSTASH_CONFLICT_PREFIX)
    from schist.sync import classify_pull_failure
    assert classify_pull_failure(output) == "conflict"
    assert not (spoke / ".git" / "rebase-merge").exists()
    assert (spoke / "notes.md").read_text() == "one\nuncommitted\nthree\n"
    assert _stash_count(spoke) == 0


def _stop_mid_rebase_with_autostash(tmp_path: Path) -> Path:
    """An explicit pull killed mid-rebase (sync_retry's 30s cap SIGKILLs the
    group): the edits then exist only in .git/rebase-merge/autostash."""
    other, spoke = _hub_and_two_clones(tmp_path)
    _remote_commit(other, "shared.md", "hub version\n")
    _local_commit(spoke, "shared.md", "conflicting local commit\n")
    (spoke / "notes.md").write_text("one\nuncommitted\nthree\n")
    _git(spoke, "pull", "--rebase", "--autostash", "origin", "main", check=False)
    assert (spoke / ".git" / "rebase-merge" / "autostash").exists()
    return spoke


def test_next_sync_cleanup_restores_edits_left_by_an_interrupted_rebase(tmp_path):
    from schist.sync import cleanup_stale_git_state

    spoke = _stop_mid_rebase_with_autostash(tmp_path)

    cleanup_stale_git_state(str(spoke), force=False)

    assert not (spoke / ".git" / "rebase-merge").exists()
    assert (spoke / "notes.md").read_text() == "one\nuncommitted\nthree\n"


def test_rebase_quit_fallback_keeps_the_autostash_in_the_stash_list(tmp_path):
    """cleanup falls back to `rebase --quit` when --abort fails. That must not
    drop the autostash; git stores it in the stash list instead."""
    spoke = _stop_mid_rebase_with_autostash(tmp_path)

    _git(spoke, "rebase", "--quit")

    assert _stash_count(spoke) == 1
    assert "uncommitted" in _git(spoke, "stash", "show", "-p").stdout


# --- sync_pull reporting --------------------------------------------------------

def _autostash_failure(monkeypatch, tmp_path, unmerged):
    from types import SimpleNamespace

    from schist import sync
    from .test_sync import _make_spoke

    vault = _make_spoke(tmp_path)
    message = (f"{git_ops.PULL_AUTOSTASH_CONFLICT_PREFIX}. They are saved in the git "
               "stash (abc); The working tree was reset to the pulled commit.\n"
               "Applying autostash resulted in conflicts.")
    monkeypatch.setattr(sync.git_ops, "pull_rebase",
                        lambda _v, autostash=False: (False, message))
    monkeypatch.setattr(sync.git_ops, "has_unmerged_entries", lambda _v: unmerged)
    rebuilt = []
    monkeypatch.setattr(sync, "_rebuild_index", lambda v, d: rebuilt.append(v))
    with pytest.raises(SystemExit) as exc:
        sync.sync_pull(SimpleNamespace(autostash=True), vault, "db.sqlite")
    return vault, exc.value.code, rebuilt


def test_sync_pull_reports_the_stash_and_rebuilds_the_index_on_a_clean_tree(
        tmp_path, monkeypatch, capsys):
    vault, code, rebuilt = _autostash_failure(monkeypatch, tmp_path, unmerged=False)

    assert code == 1
    assert rebuilt == [vault]  # HEAD moved, so the index must follow
    err = capsys.readouterr().err
    assert git_ops.PULL_AUTOSTASH_CONFLICT_PREFIX in err
    assert "MANUAL REBASE" not in err  # not the rebase-conflict guide


@pytest.mark.parametrize("unmerged", [True, None], ids=["unmerged", "unknown"])
def test_sync_pull_does_not_index_a_tree_that_may_hold_conflict_markers(
        tmp_path, monkeypatch, unmerged):
    _vault, code, rebuilt = _autostash_failure(monkeypatch, tmp_path, unmerged)

    assert code == 1
    assert rebuilt == []


def test_classifier_reads_the_autostash_prefix_only_at_line_start():
    from schist.sync import classify_pull_failure

    assert classify_pull_failure(
        git_ops.PULL_AUTOSTASH_CONFLICT_PREFIX + ". saved\nconflicts"
    ) == "autostash-conflict"
    assert classify_pull_failure(
        "CONFLICT (content): Merge conflict in notes/"
        + git_ops.PULL_AUTOSTASH_CONFLICT_PREFIX + ".md"
    ) == "conflict"


def test_staging_refuses_a_tree_with_unmerged_entries(tmp_path):
    """`git add` would mark the conflicted file resolved, markers and all."""
    other, spoke = _hub_and_two_clones(tmp_path)
    _remote_commit(other, "shared.md", "hub version\n")
    _local_commit(spoke, "notes.md", "one\ntwo\nthree\nlocal commit\n")
    (spoke / "shared.md").write_text("my uncommitted version\n")
    _post_rewrite_hook(spoke, "echo concurrent-writer > aside.md\n")
    git_ops.pull_rebase(str(spoke), autostash=True)  # leaves the tree unmerged
    assert _git(spoke, "ls-files", "--unmerged").stdout != ""

    ok, output = git_ops.stage_scope_files(str(spoke), "global")

    assert not ok
    assert "unmerged" in output
    assert _git(spoke, "ls-files", "--unmerged").stdout != ""  # nothing staged


def test_staged_edits_come_back_unstaged_after_an_aborted_autostash_pull(tmp_path):
    """Documented in hub-spoke-setup.md: git re-applies an autostash without
    --index, so the edit survives but its staged state does not."""
    other, spoke = _hub_and_two_clones(tmp_path)
    _remote_commit(other, "shared.md", "hub version\n")
    _local_commit(spoke, "shared.md", "conflicting local commit\n")
    (spoke / "notes.md").write_text("one\nstaged edit\nthree\n")
    _git(spoke, "add", "notes.md")

    ok, _output = git_ops.pull_rebase(str(spoke), autostash=True)

    assert not ok
    assert (spoke / "notes.md").read_text() == "one\nstaged edit\nthree\n"
    assert _git(spoke, "diff", "--cached", "--name-only").stdout == ""
    assert _git(spoke, "diff", "--name-only").stdout.strip() == "notes.md"


def test_quit_fallback_that_saves_the_autostash_tells_the_user_and_stops(tmp_path, capsys):
    """A killed autostash pull plus a stale index.lock: `rebase --abort`
    fails, `--quit` succeeds and stores the edits in the stash list. That
    used to return silently, leaving HEAD detached and the edits unmentioned."""
    from schist.sync import cleanup_stale_git_state

    spoke = _stop_mid_rebase_with_autostash(tmp_path)
    (spoke / ".git" / "index.lock").write_text("")

    with pytest.raises(SystemExit) as exc:
        cleanup_stale_git_state(str(spoke), force=False)

    assert exc.value.code == 1
    assert not (spoke / ".git" / "rebase-merge").exists()
    sha = _git(spoke, "rev-parse", "refs/stash").stdout.strip()[:12]
    err = capsys.readouterr().err
    assert f"git stash apply {sha}" in err
    assert "HEAD is detached" in err
    # The stash message is the LAST thing printed, so a captured stderr tail
    # (the push sentinel) carries it rather than the index.lock error.
    assert "index.lock" not in err.split("HEAD is detached", 1)[1].split("rebase --abort:", 1)[0]
    assert _stash_count(spoke) == 1
