"""Tests for spoke sync operations: init, pull, push."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from schist.spoke_config import SpokeConfig, save_spoke_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spoke(tmp_path: Path, scope: str = "research/mario") -> str:
    """Create a minimal spoke vault directory with config."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".git").mkdir()
    (vault / scope).mkdir(parents=True)
    save_spoke_config(str(vault), SpokeConfig(
        hub="git@pi.local:vault.git",
        identity="cluster-mario",
        scope=scope,
    ))
    return str(vault)


# ---------------------------------------------------------------------------
# init_spoke
# ---------------------------------------------------------------------------


class TestInitSpoke:
    def test_rejects_missing_hub(self, tmp_path, capsys):
        from schist.sync import init_spoke

        args = MagicMock(hub=None, scope="research/mario", identity="cluster-mario")
        with pytest.raises(SystemExit):
            init_spoke(args, str(tmp_path / "new"), "db.sqlite")
        assert "--hub is required" in capsys.readouterr().err

    def test_rejects_missing_scope(self, tmp_path, capsys):
        from schist.sync import init_spoke

        args = MagicMock(hub="git@host:repo.git", scope=None, identity="cluster-mario")
        with pytest.raises(SystemExit):
            init_spoke(args, str(tmp_path / "new"), "db.sqlite")
        assert "--scope is required" in capsys.readouterr().err

    def test_rejects_missing_identity(self, tmp_path, capsys):
        from schist.sync import init_spoke

        args = MagicMock(hub="git@host:repo.git", scope="research/mario", identity=None)
        with pytest.raises(SystemExit):
            init_spoke(args, str(tmp_path / "new"), "db.sqlite")
        assert "--identity is required" in capsys.readouterr().err

    def test_rejects_nonempty_dir(self, tmp_path, capsys):
        from schist.sync import init_spoke

        dest = tmp_path / "existing"
        dest.mkdir()
        (dest / "file.txt").write_text("content")
        args = MagicMock(hub="git@host:repo.git", scope="s", identity="id")
        with pytest.raises(SystemExit):
            init_spoke(args, str(dest), "db.sqlite")
        assert "already exists" in capsys.readouterr().err

    @patch("schist.sync.git_ops.clone_shallow", return_value=(True, ""))
    @patch("schist.sync.git_ops.setup_sparse_checkout", return_value=(True, ""))
    @patch("schist.sync._rebuild_index")
    def test_creates_spoke_config(self, mock_ingest, mock_sparse, mock_clone, tmp_path, capsys):
        from schist.spoke_config import is_spoke, load_spoke_config
        from schist.sync import init_spoke

        dest = str(tmp_path / "spoke")
        args = MagicMock(hub="git@pi:vault.git", scope="research/mario", identity="cluster-mario")

        # clone_shallow now receives the staging path (not `dest`); create
        # whatever path the caller passes so the mock works regardless.
        def create_at_arg(hub_url, dest_path, *a, **kw):
            Path(dest_path).mkdir(parents=True, exist_ok=True)
            (Path(dest_path) / ".git" / "info").mkdir(parents=True)
            return True, ""
        mock_clone.side_effect = create_at_arg

        init_spoke(args, dest, str(tmp_path / "db.sqlite"))

        assert is_spoke(dest)
        config = load_spoke_config(dest)
        assert config.hub == "git@pi:vault.git"
        assert config.identity == "cluster-mario"
        assert config.scope == "research/mario"

        # Verify exclude file
        exclude = Path(dest) / ".git" / "info" / "exclude"
        assert "spoke.yaml" in exclude.read_text()

        output = capsys.readouterr().out
        assert "Spoke initialized" in output

    @patch("schist.sync.git_ops.clone_shallow", return_value=(False, "Connection refused"))
    def test_clone_failure_cleans_up(self, mock_clone, tmp_path, capsys):
        from schist.sync import init_spoke

        dest = tmp_path / "spoke"
        args = MagicMock(hub="git@bad:repo.git", scope="s", identity="id")
        with pytest.raises(SystemExit):
            init_spoke(args, str(dest), "db.sqlite")
        assert not dest.exists()
        assert "clone failed" in capsys.readouterr().err

    @patch("schist.sync.git_ops.clone_shallow")
    @patch("schist.sync.git_ops.setup_sparse_checkout",
           return_value=(False, "scope path produces empty checkout"))
    def test_sparse_checkout_failure_leaves_no_target_dir(
        self, mock_sparse, mock_clone, tmp_path, capsys
    ):
        """Issue #41: failure mid-init must leave the target dir absent so
        the user can re-run init without `rm -rf`."""
        from schist.sync import init_spoke

        dest = tmp_path / "spoke"

        # clone_shallow gets called with the staging path, not `dest`.
        # Create whatever directory the function asks for, return success.
        def create_at_arg(hub_url, dest_path, *a, **kw):
            Path(dest_path).mkdir(parents=True, exist_ok=True)
            (Path(dest_path) / ".git" / "info").mkdir(parents=True)
            return True, ""
        mock_clone.side_effect = create_at_arg

        args = MagicMock(
            hub="git@pi:vault.git", scope="bad/scope", identity="cluster",
        )
        with pytest.raises(SystemExit):
            init_spoke(args, str(dest), str(tmp_path / "db.sqlite"))

        # Target dir absent (or empty) — user can re-run.
        assert not dest.exists() or not any(dest.iterdir())

        # No leftover staging dir in the parent.
        leftovers = [
            p for p in tmp_path.iterdir()
            if p.name.startswith(".spoke.init-")
        ]
        assert leftovers == [], f"staging leftovers: {leftovers}"

        assert "sparse checkout failed" in capsys.readouterr().err

    @patch("schist.sync.git_ops.clone_shallow")
    @patch("schist.sync.git_ops.setup_sparse_checkout", return_value=(True, ""))
    @patch("schist.sync._install_local_hooks", side_effect=OSError("disk full"))
    def test_hook_install_failure_leaves_no_target_dir(
        self, mock_hooks, mock_sparse, mock_clone, tmp_path, capsys
    ):
        """Failure in a step that today has NO cleanup (hooks install) must
        also leave the target dir absent under the staging refactor."""
        from schist.sync import init_spoke

        dest = tmp_path / "spoke"

        def create_at_arg(hub_url, dest_path, *a, **kw):
            Path(dest_path).mkdir(parents=True, exist_ok=True)
            (Path(dest_path) / ".git" / "info").mkdir(parents=True)
            return True, ""
        mock_clone.side_effect = create_at_arg

        args = MagicMock(
            hub="git@pi:vault.git", scope="research/x", identity="cluster",
        )
        with pytest.raises(SystemExit):
            init_spoke(args, str(dest), str(tmp_path / "db.sqlite"))

        assert not dest.exists() or not any(dest.iterdir())
        leftovers = [
            p for p in tmp_path.iterdir()
            if p.name.startswith(".spoke.init-")
        ]
        assert leftovers == []

    @patch("schist.sync.git_ops.clone_shallow")
    @patch("schist.sync.git_ops.setup_sparse_checkout", return_value=(True, ""))
    @patch("schist.sync._install_local_hooks", side_effect=OSError("disk full"))
    @patch("schist.sync.shutil.rmtree", side_effect=OSError("rmtree denied"))
    def test_cleanup_failure_surfaces_manual_fix_hint(
        self, mock_rmtree, mock_hooks, mock_sparse, mock_clone, tmp_path, capsys
    ):
        """When BOTH the build and the cleanup fail, surface a 'Manual fix:
        rm -rf <path>' hint so the user can recover."""
        from schist.sync import init_spoke

        dest = tmp_path / "spoke"

        def create_at_arg(hub_url, dest_path, *a, **kw):
            Path(dest_path).mkdir(parents=True, exist_ok=True)
            (Path(dest_path) / ".git" / "info").mkdir(parents=True)
            return True, ""
        mock_clone.side_effect = create_at_arg

        args = MagicMock(
            hub="git@pi:vault.git", scope="research/x", identity="cluster",
        )
        with pytest.raises(SystemExit):
            init_spoke(args, str(dest), str(tmp_path / "db.sqlite"))

        err = capsys.readouterr().err
        assert "disk full" in err  # original error preserved
        assert "Manual fix: rm -rf" in err  # cleanup-failure hint

    def test_final_install_race_cleans_staging(self, tmp_path, monkeypatch, capsys):
        """If the target becomes non-empty after the pre-check, init_spoke
        should clean staging and report a user-facing install error."""
        from schist import sync as sync_mod

        dest = tmp_path / "spoke"

        def fake_build(staging, hub, scope, identity):
            staging.mkdir(parents=True)
            (staging / ".git" / "info").mkdir(parents=True)
            dest.mkdir()
            (dest / "intruder").write_text("raced\n")

        monkeypatch.setattr(sync_mod, "_build_spoke_in_staging", fake_build)

        args = MagicMock(hub="git@pi:vault.git", scope="research/x", identity="cluster")
        with pytest.raises(SystemExit):
            sync_mod.init_spoke(args, str(dest), str(tmp_path / "db.sqlite"))

        err = capsys.readouterr().err
        assert "failed to install vault" in err
        assert not list(tmp_path.glob(".spoke.init-*"))
        assert (dest / "intruder").read_text() == "raced\n"

    @patch("schist.sync.git_ops.clone_shallow", return_value=(True, ""))
    @patch("schist.sync.git_ops.setup_sparse_checkout", return_value=(True, ""))
    @patch("schist.sync._rebuild_index")
    def test_installs_local_hooks(self, mock_ingest, mock_sparse, mock_clone, tmp_path):
        """Spoke init must install the post-commit + pre-commit hooks so the
        spoke behaves like a standalone vault for local commits — without
        them, post-commit ingest never fires and staged secrets aren't blocked.
        """
        from schist.sync import init_spoke

        dest = str(tmp_path / "spoke")

        # clone_shallow now receives the staging path (not `dest`); create
        # whatever path the caller passes so the mock works regardless.
        def create_at_arg(hub_url, dest_path, *a, **kw):
            Path(dest_path).mkdir(parents=True, exist_ok=True)
            (Path(dest_path) / ".git" / "info").mkdir(parents=True)
            return True, ""
        mock_clone.side_effect = create_at_arg

        args = MagicMock(hub="git@pi:vault.git", scope="research/x", identity="x")
        init_spoke(args, dest, str(tmp_path / "db.sqlite"))

        post = Path(dest) / ".git" / "hooks" / "post-commit"
        pre = Path(dest) / ".git" / "hooks" / "pre-commit"
        assert post.is_file() and "schist post-commit" in post.read_text()
        assert pre.is_file() and "schist pre-commit" in pre.read_text()
        # Hooks must be executable for git to invoke them.
        assert post.stat().st_mode & 0o111
        assert pre.stat().st_mode & 0o111


# ---------------------------------------------------------------------------
# sync_pull
# ---------------------------------------------------------------------------


class TestSyncPull:
    def test_non_spoke_fails(self, tmp_path, capsys):
        from schist.sync import sync_pull

        vault = tmp_path / "vault"
        vault.mkdir()
        args = MagicMock()
        with pytest.raises(SystemExit):
            sync_pull(args, str(vault), "db.sqlite")
        assert "not a spoke vault" in capsys.readouterr().err

    @patch("schist.sync.git_ops.pull_rebase", return_value=(True, "Already up to date."))
    @patch("schist.sync._rebuild_index")
    def test_pull_rebuilds_db(self, mock_ingest, mock_pull, tmp_path, capsys):
        from schist.sync import sync_pull

        vault = _make_spoke(tmp_path)
        db = Path(vault) / ".schist" / "schist.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_text("old data")

        args = MagicMock()
        sync_pull(args, vault, str(db))

        # DB should have been deleted before rebuild
        mock_ingest.assert_called_once()
        assert "Pull complete" in capsys.readouterr().out

    @patch("schist.sync.git_ops.pull_rebase", return_value=(True, ""))
    @patch("schist.sync._rebuild_index")
    @patch("subprocess.run")
    def test_pull_heals_leftover_rebase(self, mock_run, mock_ingest, mock_pull, tmp_path, capsys):
        """sync_pull aborts a prior half-rebase (e.g. from a SIGKILL'd MCP pull)."""
        from schist.sync import sync_pull

        vault = _make_spoke(tmp_path)
        # Simulate a leftover rebase directory from a killed pull
        (Path(vault) / ".git" / "rebase-merge").mkdir(parents=True)

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        args = MagicMock()
        sync_pull(args, vault, "db.sqlite")

        # subprocess.run should have been called with git rebase --abort
        called_with_abort = any(
            ("rebase" in (call.args[0] if call.args else []) and
             "--abort" in (call.args[0] if call.args else []))
            for call in mock_run.call_args_list
        )
        assert called_with_abort, f"expected rebase --abort, got calls: {mock_run.call_args_list}"
        assert "Aborting leftover rebase" in capsys.readouterr().err

    @patch("schist.sync.git_ops.pull_rebase", return_value=(True, ""))
    @patch("schist.sync._rebuild_index")
    @patch("subprocess.run")
    def test_pull_falls_back_to_rebase_quit(self, mock_run, mock_ingest, mock_pull, tmp_path, capsys):
        """If `rebase --abort` fails, sync_pull tries `rebase --quit` as fallback."""
        from schist.sync import sync_pull

        vault = _make_spoke(tmp_path)
        (Path(vault) / ".git" / "rebase-apply").mkdir(parents=True)

        # First subprocess.run (rebase --abort) fails; second (rebase --quit) succeeds.
        mock_run.side_effect = [
            MagicMock(returncode=128, stdout="", stderr="fatal: no rebase in progress"),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        args = MagicMock()
        sync_pull(args, vault, "db.sqlite")  # should not sys.exit

        calls = [call.args[0] for call in mock_run.call_args_list if call.args]
        assert ["git", "rebase", "--abort"] in calls
        assert ["git", "rebase", "--quit"] in calls

    @patch("schist.sync._rebuild_index")
    @patch("subprocess.run")
    def test_pull_exits_when_rebase_cleanup_fails(self, mock_run, mock_ingest, tmp_path, capsys):
        """If both --abort and --quit fail, sync_pull exits with a clear error."""
        from schist.sync import sync_pull

        vault = _make_spoke(tmp_path)
        (Path(vault) / ".git" / "rebase-merge").mkdir(parents=True)

        # Both abort and quit fail
        mock_run.return_value = MagicMock(returncode=128, stdout="", stderr="git is confused")

        args = MagicMock()
        with pytest.raises(SystemExit):
            sync_pull(args, vault, "db.sqlite")

        err = capsys.readouterr().err
        assert "could not clear rebase state" in err
        assert "Manual fix" in err

    @patch(
        "schist.sync.git_ops.pull_rebase",
        return_value=(
            False,
            "Auto-merging research/mario/a.md\n"
            "CONFLICT (content): Merge conflict in research/mario/a.md\n"
            "Auto-merging research/mario/b.md\n"
            "CONFLICT (content): Merge conflict in research/mario/b.md\n"
            "error: Failed to merge in the changes.",
        ),
    )
    def test_pull_conflict_rich_recovery_message(self, mock_pull, tmp_path, capsys):
        """On conflict, sync_pull prints conflicting-file list + 3 recovery
        paths (INSPECT, MANUAL REBASE, RE-CLONE) and explicitly tells the
        user their local state is unchanged."""
        from schist.sync import sync_pull

        vault = _make_spoke(tmp_path)
        args = MagicMock()
        with pytest.raises(SystemExit):
            sync_pull(args, vault, "db.sqlite")
        err = capsys.readouterr().err

        # Core message: what happened + that local state is safe
        assert "pull failed with conflicts" in err
        assert "Local state is unchanged" in err
        assert "auto-aborted" in err

        # Both conflicting files appear in the list
        assert "research/mario/a.md" in err
        assert "research/mario/b.md" in err

        # All three recovery paths are presented
        assert "INSPECT" in err
        assert "MANUAL REBASE" in err
        assert "RE-CLONE" in err

        # Re-clone option shows the exact re-init command with spoke identity
        assert "schist init --spoke" in err
        assert "cluster-mario" in err  # identity from _make_spoke()
        assert "git@pi.local:vault.git" in err  # hub URL from _make_spoke()

    def test_pull_non_conflict_error_keeps_raw_output(self, tmp_path, capsys):
        """Non-conflict errors (e.g. network failure) skip the rich recovery
        block and just surface the raw git output."""
        from schist.sync import sync_pull

        vault = _make_spoke(tmp_path)
        args = MagicMock()
        with patch(
            "schist.sync.git_ops.pull_rebase",
            return_value=(False, "fatal: Could not resolve hostname pi.local"),
        ), pytest.raises(SystemExit):
            sync_pull(args, vault, "db.sqlite")
        err = capsys.readouterr().err
        assert "Error: pull failed" in err
        assert "Could not resolve hostname" in err
        # None of the rich-recovery headers should appear
        assert "INSPECT" not in err
        assert "RE-CLONE" not in err

    def test_extract_conflicting_files_parses_git_output(self):
        """Unit test on the helper: various CONFLICT line shapes are matched."""
        from schist.sync import _extract_conflicting_files

        output = (
            "Auto-merging a.md\n"
            "CONFLICT (content): Merge conflict in a.md\n"
            "CONFLICT (modify/delete): b.md deleted in HEAD and modified in commit\n"
            "CONFLICT (content): Merge conflict in nested/dir/c.md\n"
            "error: Failed to merge in the changes."
        )
        files = _extract_conflicting_files(output)
        # Only "content"-style conflicts are matched (intentional — that's
        # what the regex targets). modify/delete is a different shape.
        assert files == ["a.md", "nested/dir/c.md"]

    def test_extract_conflicting_files_deduplicates(self):
        """Duplicate conflict lines are deduplicated in the returned list."""
        from schist.sync import _extract_conflicting_files

        output = (
            "CONFLICT (content): Merge conflict in foo.md\n"
            "CONFLICT (content): Merge conflict in foo.md\n"
            "CONFLICT (content): Merge conflict in bar.md\n"
        )
        assert _extract_conflicting_files(output) == ["foo.md", "bar.md"]

    def test_extract_conflicting_files_empty_on_no_match(self):
        """If git output has no CONFLICT lines (unusual but possible),
        return an empty list rather than raising."""
        from schist.sync import _extract_conflicting_files

        assert _extract_conflicting_files("fatal: something else") == []
        assert _extract_conflicting_files("") == []


# ---------------------------------------------------------------------------
# sync_push
# ---------------------------------------------------------------------------


class TestSyncPush:
    def test_non_spoke_fails(self, tmp_path, capsys):
        from schist.sync import sync_push

        vault = tmp_path / "vault"
        vault.mkdir()
        args = MagicMock()
        with pytest.raises(SystemExit):
            sync_push(args, str(vault), "db.sqlite")
        assert "not a spoke vault" in capsys.readouterr().err

    @patch("schist.sync.git_ops.has_unpushed_commits", return_value=False)
    @patch("schist.sync.git_ops.has_uncommitted_changes", return_value=False)
    def test_nothing_to_push(self, mock_changes, mock_unpushed, tmp_path, capsys):
        from schist.sync import sync_push

        vault = _make_spoke(tmp_path)
        args = MagicMock()
        sync_push(args, vault, "db.sqlite")
        assert "Nothing to push" in capsys.readouterr().out

    @patch("schist.sync.git_ops.push", return_value=(True, ""))
    @patch("schist.sync.git_ops.has_unpushed_commits", return_value=True)
    @patch("schist.sync.git_ops.commit", return_value=(True, ""))
    @patch("schist.sync.git_ops.stage_scope_files", return_value=(True, ""))
    @patch("schist.sync.git_ops.has_uncommitted_changes", return_value=True)
    @patch("subprocess.run")
    def test_auto_commits_and_pushes(self, mock_run, mock_changes, mock_stage, mock_commit,
                                     mock_unpushed, mock_push, tmp_path, capsys):
        from schist.sync import sync_push

        # Mock git diff --cached --name-only
        mock_run.return_value = MagicMock(stdout="research/mario/note.md\n", returncode=0)

        vault = _make_spoke(tmp_path)
        args = MagicMock()
        sync_push(args, vault, "db.sqlite")

        mock_commit.assert_called_once()
        commit_msg = mock_commit.call_args[0][1]
        assert "sync(cluster-mario)" in commit_msg
        mock_push.assert_called_once()
        assert "Pushed to hub" in capsys.readouterr().out

    @patch("schist.sync.git_ops.has_uncommitted_changes", return_value=True)
    @patch("schist.sync.git_ops.stage_scope_files", return_value=(False, "fatal: pathspec 'global/' did not match"))
    def test_stage_failure_is_reported(self, mock_stage, mock_changes, tmp_path, capsys):
        from schist.sync import sync_push

        vault = _make_spoke(tmp_path)
        args = MagicMock()

        with pytest.raises(SystemExit) as exc:
            sync_push(args, vault, "db.sqlite")

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "failed to stage scope" in err
        assert "pathspec" in err

    @patch("schist.sync.git_ops.push", return_value=(False, "REJECTED: push contains out-of-scope writes"))
    @patch("schist.sync.git_ops.has_unpushed_commits", return_value=True)
    @patch("schist.sync.git_ops.has_uncommitted_changes", return_value=False)
    def test_push_rejection(self, mock_changes, mock_unpushed, mock_push, tmp_path, capsys):
        from schist.sync import sync_push

        vault = _make_spoke(tmp_path)
        args = MagicMock()
        with pytest.raises(SystemExit):
            sync_push(args, vault, "db.sqlite")
        assert "rejected" in capsys.readouterr().err.lower()

    @patch("schist.sync.git_ops.push", return_value=(False, "fatal: Could not resolve hostname"))
    @patch("schist.sync.git_ops.has_unpushed_commits", return_value=True)
    @patch("schist.sync.git_ops.has_uncommitted_changes", return_value=False)
    def test_hub_unreachable(self, mock_changes, mock_unpushed, mock_push, tmp_path, capsys):
        from schist.sync import sync_push

        vault = _make_spoke(tmp_path)
        args = MagicMock()
        with pytest.raises(SystemExit):
            sync_push(args, vault, "db.sqlite")
        err = capsys.readouterr().err
        assert "unreachable" in err.lower() or "saved locally" in err.lower()

    @patch("schist.sync.git_ops.has_uncommitted_changes", return_value=False)
    def test_push_blocks_index_lock_without_force(self, mock_changes, tmp_path, capsys):
        from schist.sync import sync_push

        vault = _make_spoke(tmp_path)
        (Path(vault) / ".git" / "index.lock").write_text("")
        args = MagicMock(force=False)

        with pytest.raises(SystemExit):
            sync_push(args, vault, "db.sqlite")

        assert "index.lock" in capsys.readouterr().err

    @patch("schist.sync.git_ops.has_unpushed_commits", return_value=False)
    @patch("schist.sync.git_ops.has_uncommitted_changes", return_value=False)
    def test_push_force_removes_index_lock(self, mock_changes, mock_unpushed, tmp_path, capsys):
        from schist.sync import sync_push

        vault = _make_spoke(tmp_path)
        lock = Path(vault) / ".git" / "index.lock"
        lock.write_text("")
        args = MagicMock(force=True)

        sync_push(args, vault, "db.sqlite")

        assert not lock.exists()
        assert "Removing stale git index.lock" in capsys.readouterr().err

    @patch("schist.sync.git_ops.has_unpushed_commits", return_value=False)
    @patch("schist.sync.git_ops.has_uncommitted_changes", return_value=False)
    @patch("subprocess.run")
    def test_push_force_aborts_merge_state(
        self, mock_run, mock_changes, mock_unpushed, tmp_path, capsys
    ):
        from schist.sync import sync_push

        vault = _make_spoke(tmp_path)
        (Path(vault) / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        args = MagicMock(force=True)

        sync_push(args, vault, "db.sqlite")

        calls = [call.args[0] for call in mock_run.call_args_list if call.args]
        assert ["git", "merge", "--abort"] in calls
        assert "Aborting leftover merge state" in capsys.readouterr().err

    @patch("schist.sync.git_ops.has_unpushed_commits", return_value=False)
    @patch("schist.sync.git_ops.has_uncommitted_changes", return_value=False)
    @patch("subprocess.run")
    def test_push_force_removes_lock_before_merge_abort(
        self, mock_run, mock_changes, mock_unpushed, tmp_path, capsys
    ):
        from schist.sync import sync_push

        vault = _make_spoke(tmp_path)
        git_dir = Path(vault) / ".git"
        lock = git_dir / "index.lock"
        lock.write_text("")
        (git_dir / "MERGE_HEAD").write_text("deadbeef\n")

        def merge_abort(args, **kwargs):
            assert args == ["git", "merge", "--abort"]
            assert not lock.exists()
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = merge_abort
        args = MagicMock(force=True)

        sync_push(args, vault, "db.sqlite")

        err = capsys.readouterr().err
        assert "Removing stale git index.lock" in err
        assert "Aborting leftover merge state" in err

    @patch("schist.sync.git_ops.has_uncommitted_changes", return_value=False)
    @patch("subprocess.run")
    def test_push_force_keeps_merge_head_when_abort_fails(
        self, mock_run, mock_changes, tmp_path, capsys
    ):
        from schist.sync import sync_push

        vault = _make_spoke(tmp_path)
        merge_head = Path(vault) / ".git" / "MERGE_HEAD"
        merge_head.write_text("deadbeef\n")
        mock_run.return_value = MagicMock(returncode=128, stdout="", stderr="fatal: unresolved merge")
        args = MagicMock(force=True)

        with pytest.raises(SystemExit):
            sync_push(args, vault, "db.sqlite")

        assert merge_head.exists()
        err = capsys.readouterr().err
        assert "could not clear merge state" in err
        assert "resolve or abort the merge manually" in err


class TestStageScopeFiles:
    def test_global_scope_stages_existing_canonical_dirs(self, tmp_path):
        from schist import git_ops

        vault = tmp_path / "vault"
        vault.mkdir()
        subprocess.run(["git", "init"], cwd=vault, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=vault, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=vault, check=True)
        (vault / "concepts").mkdir()
        (vault / "notes").mkdir()
        (vault / "concepts" / "existing.md").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "concepts/existing.md"], cwd=vault, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=vault, check=True, capture_output=True, text=True)

        (vault / "concepts" / "existing.md").write_text("after\n", encoding="utf-8")

        ok, output = git_ops.stage_scope_files(str(vault), "global")

        assert ok, output
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=vault, check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        assert staged == ["concepts/existing.md"]

    def test_global_scope_with_no_content_dirs_is_noop_success(self, tmp_path):
        from schist import git_ops

        vault = tmp_path / "vault"
        vault.mkdir()
        subprocess.run(["git", "init"], cwd=vault, check=True, capture_output=True, text=True)

        ok, output = git_ops.stage_scope_files(str(vault), "global")

        assert ok is True
        assert output == ""

    def test_global_scope_stages_deleted_tracked_file(self, tmp_path):
        from schist import git_ops

        vault = tmp_path / "vault"
        vault.mkdir()
        subprocess.run(["git", "init"], cwd=vault, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=vault, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=vault, check=True)
        (vault / "concepts").mkdir()
        tracked = vault / "concepts" / "gone.md"
        tracked.write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "concepts/gone.md"], cwd=vault, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=vault, check=True, capture_output=True, text=True)
        tracked.unlink()
        (vault / "concepts").rmdir()

        ok, output = git_ops.stage_scope_files(str(vault), "global")

        assert ok, output
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-status"],
            cwd=vault, check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert staged == "D\tconcepts/gone.md"


# ---------------------------------------------------------------------------
# _rebuild_index side-table preservation
# ---------------------------------------------------------------------------


def _init_vault_with_schema(
    tmp_path: Path, *, vault_yaml_body: str | None = None
) -> tuple[str, str]:
    """Set up a minimal vault + run a real ingest so schist.db exists with
    the current schema.sql applied. Returns (vault_path, db_path).

    If `vault_yaml_body` is provided, it's written to vault.yaml before the
    first rebuild.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "notes").mkdir()
    (vault / "concepts").mkdir()
    # One real markdown file so ingest has something to insert into docs
    (vault / "notes" / "2026-01-01-seed.md").write_text(
        "---\ntitle: seed\ndate: '2026-01-01'\nstatus: draft\n---\n\nSeed body.\n"
    )
    if vault_yaml_body is not None:
        (vault / "vault.yaml").write_text(vault_yaml_body)
    db_path = str(vault / ".schist" / "schist.db")
    (vault / ".schist").mkdir()

    # Run _rebuild_index once to lay down the schema + seed doc row
    from schist.sync import _rebuild_index

    _rebuild_index(str(vault), db_path)
    if not Path(db_path).exists():
        pytest.skip("ingest.py unavailable — can't set up the rebuild preservation tests")
    return str(vault), db_path


class TestRebuildIndexSideTablePreservation:
    def test_concept_aliases_survive_rebuild(self, tmp_path):
        """Rows in concept_aliases must survive a second _rebuild_index."""
        import sqlite3
        from schist.sync import _rebuild_index

        vault, db_path = _init_vault_with_schema(tmp_path)
        concepts = Path(vault) / "concepts"
        concepts.mkdir(exist_ok=True)
        (concepts / "backprop.md").write_text(
            "---\nconcept: backprop\ntitle: Backprop\n---\n\nShort form.\n"
        )
        (concepts / "backpropagation.md").write_text(
            "---\nconcept: backpropagation\ntitle: Backpropagation\n---\n\nCanonical.\n"
        )
        _rebuild_index(vault, db_path)

        # Insert a concept_alias row into the existing DB
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO concept_aliases "
                "(duplicate_slug, canonical_slug, reason, created_by) "
                "VALUES (?, ?, ?, ?)",
                ("backprop", "backpropagation", "short form", "tester"),
            )
            conn.commit()
        finally:
            conn.close()

        # Rebuild (simulates spoke pull)
        _rebuild_index(vault, db_path)

        # Row should still be there
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT duplicate_slug, canonical_slug, reason, created_by "
                "FROM concept_aliases"
            ).fetchall()
        finally:
            conn.close()

        assert rows == [("backprop", "backpropagation", "short form", "tester")]

    def test_stale_concept_alias_pruned_on_rebuild(self, tmp_path):
        """A dangling concept_alias (endpoint not in the rebuilt concepts
        table) must NOT survive a rebuild — the rebuild/spoke-pull path must
        prune it just like the in-place ingest commit path. Regression for
        issue #213."""
        import sqlite3
        from schist.sync import _rebuild_index

        vault, db_path = _init_vault_with_schema(tmp_path)
        concepts = Path(vault) / "concepts"
        concepts.mkdir(exist_ok=True)
        # Only ONE endpoint exists as a concept; the alias below references a
        # canonical_slug ("backpropagation") that has no concept file, so it is
        # a dangling-FK row once the concepts table is rebuilt.
        (concepts / "backprop.md").write_text(
            "---\nconcept: backprop\ntitle: Backprop\n---\n\nShort form.\n"
        )
        _rebuild_index(vault, db_path)

        # Insert a dangling alias directly (bypasses ingest's prune)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO concept_aliases "
                "(duplicate_slug, canonical_slug, reason, created_by) "
                "VALUES (?, ?, ?, ?)",
                ("backprop", "backpropagation", "dangling", "tester"),
            )
            conn.commit()
        finally:
            conn.close()

        # Rebuild (simulates spoke pull) — the dangling row must be pruned,
        # not copied forward.
        _rebuild_index(vault, db_path)

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM concept_aliases"
            ).fetchone()[0]
        finally:
            conn.close()

        assert count == 0, "dangling alias survived rebuild — #213 regression"

    def test_rebuild_ok_with_no_prior_db(self, tmp_path):
        """First rebuild (no backup) should succeed with no side-table data."""
        import sqlite3
        from schist.sync import _rebuild_index

        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "notes").mkdir()
        (vault / ".schist").mkdir()
        (vault / "notes" / "2026-01-01-seed.md").write_text(
            "---\ntitle: seed\ndate: '2026-01-01'\nstatus: draft\n---\n\nBody.\n"
        )
        db_path = str(vault / ".schist" / "schist.db")

        _rebuild_index(str(vault), db_path)

        if not Path(db_path).exists():
            pytest.skip("ingest.py unavailable")

        conn = sqlite3.connect(db_path)
        try:
            # Side table should exist and be empty
            assert conn.execute("SELECT COUNT(*) FROM concept_aliases").fetchone()[0] == 0
        finally:
            conn.close()

    def test_rebuild_handles_missing_side_table_in_backup(self, tmp_path):
        """Older DB format without `concept_aliases` should not crash the
        rebuild — the missing table should be skipped silently."""
        import sqlite3
        from schist.sync import _rebuild_index

        vault, db_path = _init_vault_with_schema(tmp_path)

        # Simulate an older DB format by dropping concept_aliases
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DROP TABLE concept_aliases")
            conn.commit()
        finally:
            conn.close()

        # Rebuild should still succeed (missing table in backup is skipped)
        _rebuild_index(vault, db_path)

        # New DB should have the fresh (empty) concept_aliases table
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM concept_aliases").fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_side_table_columns_list_is_complete(self, tmp_path):
        """Guard test: if schema.sql adds a column to concept_aliases, the
        hardcoded column list in sync.py must be updated too. This test
        reads the actual schema from a fresh DB and compares against the
        hardcoded list.
        """
        import sqlite3
        from schist.sync import _SIDE_TABLE_COLUMNS

        vault, db_path = _init_vault_with_schema(tmp_path)

        conn = sqlite3.connect(db_path)
        try:
            for table, expected_cols in _SIDE_TABLE_COLUMNS.items():
                rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
                actual_cols = tuple(r[1] for r in rows)
                assert actual_cols == expected_cols, (
                    f"{table}: schema.sql and sync.py disagree.\n"
                    f"  schema.sql columns: {actual_cols}\n"
                    f"  sync.py columns:    {expected_cols}\n"
                    f"  Update _SIDE_TABLE_COLUMNS in cli/schist/sync.py."
                )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Seed-vault template — flat default + content-axis writes
# ---------------------------------------------------------------------------


class TestBuildSeedVault:
    def test_seed_uses_flat_scope_convention(self):
        from schist.sync import _build_seed_vault

        data = _build_seed_vault(name="hub-x", participants=["alice", "bob"])
        assert data["scope_convention"] == "flat"

    def test_seed_participants_default_scope_global(self):
        from schist.sync import _build_seed_vault

        data = _build_seed_vault(name="hub-x", participants=["alice", "bob"])
        for p in data["participants"]:
            assert p["default_scope"] == "global", p

    def test_seed_write_list_is_content_axis(self):
        from schist.sync import _build_seed_vault

        data = _build_seed_vault(name="hub-x", participants=["alice"])
        expected = ["research", "concepts", "decisions", "notes", "ops", "papers"]
        assert data["access"]["alice"]["write"] == expected

    def test_seed_validates_under_acl_parser(self):
        """Generated seed must round-trip through parse_vault_data without errors."""
        from schist.acl import parse_vault_data
        from schist.sync import _build_seed_vault

        data = _build_seed_vault(name="hub-x", participants=["alice", "bob"])
        acl = parse_vault_data(data)
        assert acl.scope_convention == "flat"
        assert acl.get_participant("alice").default_scope == "global"


class TestBuildStandaloneVault:
    def test_standalone_uses_flat_scope_convention(self):
        from schist.sync import _build_standalone_vault

        data = _build_standalone_vault(name="v", identity="local")
        assert data["scope_convention"] == "flat"

    def test_standalone_validates_under_acl_parser(self):
        from schist.acl import parse_vault_data
        from schist.sync import _build_standalone_vault

        data = _build_standalone_vault(name="v", identity="local")
        acl = parse_vault_data(data)
        assert acl.scope_convention == "flat"


class TestRebuildIndexWalSafety:
    """#254 follow-up: _rebuild_index must move/delete the -wal/-shm siblings
    with the main DB file, or a WAL DB whose close-checkpoint was blocked
    (an MCP reader open at ingest-close time) silently loses its index."""

    def test_aliases_survive_rebuild_when_index_lives_in_wal(self, tmp_path):
        import sqlite3
        from schist.sync import _rebuild_index

        vault, db_path = _init_vault_with_schema(tmp_path)
        # Alias endpoints must exist as concepts or the #213 dangling-FK
        # prune (not the WAL handling under test) removes the row.
        concepts = Path(vault) / "concepts"
        (concepts / "bp.md").write_text(
            "---\nconcept: bp\ntitle: BP\n---\n\nShort form.\n"
        )
        (concepts / "backpropagation.md").write_text(
            "---\nconcept: backpropagation\ntitle: Backpropagation\n---\n\nCanonical.\n"
        )
        _rebuild_index(vault, db_path)

        # Put an alias row in, then force the "entire index in the -wal"
        # state: a read-only connection with an active statement blocks the
        # writer's close-checkpoint, and a read-only close can't checkpoint.
        writer = sqlite3.connect(db_path)
        writer.execute(
            "INSERT INTO concept_aliases "
            "(duplicate_slug, canonical_slug, reason, created_by) "
            "VALUES ('bp', 'backpropagation', 'short', 'tester')"
        )
        writer.commit()
        reader = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = reader.execute("SELECT id FROM docs")
        writer.close()
        cursor.fetchall()
        reader.close()

        wal = Path(f"{db_path}-wal")
        assert wal.exists() and wal.stat().st_size > 0, (
            "test precondition failed: expected the index to be left in the -wal"
        )

        _rebuild_index(vault, db_path)

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT duplicate_slug, canonical_slug FROM concept_aliases"
            ).fetchall()
        finally:
            conn.close()
        assert ("bp", "backpropagation") in rows
        # No orphaned backup or sibling files left behind
        assert not Path(f"{db_path}.bak").exists()
        assert not Path(f"{db_path}.bak-wal").exists()

    def test_failed_rebuild_restores_backup_without_replaying_stray_wal(self, tmp_path, capsys):
        import sqlite3
        from unittest.mock import patch as _patch
        from schist.sync import _rebuild_index

        vault, db_path = _init_vault_with_schema(tmp_path)
        conn = sqlite3.connect(db_path)
        old_docs = conn.execute("SELECT id FROM docs ORDER BY id").fetchall()
        conn.close()

        def poison_then_fail(vault_path, db):
            # Mimic a rebuild that dies after ingest wrote at the live path
            # (e.g. _preserve_side_tables raising): a fresh WAL DB whose data
            # sits in its -wal file.
            c = sqlite3.connect(db)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE junk (x)")
            c.execute("INSERT INTO junk VALUES (1)")
            c.commit()
            # Leave the connection open so close-checkpointing can't fold the
            # -wal back into the main file, then abandon it.
            raise RuntimeError("boom after partial write")

        with _patch("schist.sqlite_query._run_ingest", side_effect=poison_then_fail):
            _rebuild_index(vault, db_path)

        assert "index rebuild failed" in capsys.readouterr().err
        conn = sqlite3.connect(db_path)
        try:
            docs = conn.execute("SELECT id FROM docs ORDER BY id").fetchall()
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert docs == old_docs
        assert "junk" not in tables, "failed ingest's -wal was replayed into the restored backup"
        assert not Path(f"{db_path}.bak").exists()


class TestMoveDbWithWalAtomicity:
    """#254 /review finding: _move_db_with_wal must not leave a half-moved
    set (a stray -wal at a path a fresh DB later reuses) if one rename fails."""

    def test_partial_move_rolls_back_to_source(self, tmp_path, monkeypatch):
        import schist.sync as sync

        src = tmp_path / "schist.db"
        dst = tmp_path / "schist.db.bak"
        src.write_bytes(b"main")
        Path(f"{src}-wal").write_bytes(b"wal")
        Path(f"{src}-shm").write_bytes(b"shm")

        real_rename = Path.rename

        def flaky_rename(self, target):
            # Fail specifically on the main-file move (siblings move first),
            # so at least one sibling has already been renamed to dst.
            if self == src:
                raise OSError("simulated rename failure")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", flaky_rename)
        with pytest.raises(OSError):
            sync._move_db_with_wal(src, dst)
        monkeypatch.undo()

        # Everything rolled back to src; nothing orphaned at dst.
        assert src.exists() and Path(f"{src}-wal").exists() and Path(f"{src}-shm").exists()
        assert not dst.exists()
        assert not Path(f"{dst}-wal").exists()
        assert not Path(f"{dst}-shm").exists()

    def test_rebuild_aborts_intact_when_backup_move_fails(self, tmp_path, monkeypatch, capsys):
        import sqlite3
        import schist.sync as sync

        vault, db_path = _init_vault_with_schema(tmp_path)
        conn = sqlite3.connect(db_path)
        before = conn.execute("SELECT id FROM docs ORDER BY id").fetchall()
        conn.close()

        def always_fail(src, dst):
            raise OSError("simulated backup failure")

        monkeypatch.setattr(sync, "_move_db_with_wal", always_fail)
        sync._rebuild_index(vault, db_path)
        monkeypatch.undo()

        assert "rebuild skipped (backup failed)" in capsys.readouterr().err
        conn = sqlite3.connect(db_path)
        try:
            after = conn.execute("SELECT id FROM docs ORDER BY id").fetchall()
        finally:
            conn.close()
        assert after == before  # existing index left intact, not overwritten


# ---------------------------------------------------------------------------
# _run_git_cleanup timeouts (#321)
# ---------------------------------------------------------------------------


class TestRunGitCleanupTimeout:
    def test_cleanup_commands_carry_timeout(self):
        from schist.sync import _run_git_cleanup

        calls = []

        def _record(*args, **kwargs):
            calls.append((args[0], kwargs.get("timeout")))
            return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=_record):
            _run_git_cleanup("/tmp/vault", ["merge", "--abort"])

        assert calls, "expected a git subprocess call"
        for argv, timeout in calls:
            assert timeout is not None and timeout > 0, f"{argv} ran with no timeout"

    def test_timeout_returns_failed_process_not_exception(self):
        """A stalled abort must come back as a failed CompletedProcess so the
        callers' existing returncode/stderr handling still applies."""
        from schist.sync import _run_git_cleanup

        def _stall(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

        with patch("subprocess.run", side_effect=_stall):
            result = _run_git_cleanup("/tmp/vault", ["rebase", "--abort"])

        assert result.returncode != 0
        assert "timed out" in result.stderr

    def test_stalled_abort_falls_back_to_rebase_quit(self, tmp_path, capsys):
        """The abort-times-out path must reach the existing --quit fallback
        instead of hanging or crashing."""
        from schist.sync import _cleanup_rebase_state

        def _abort_stalls_quit_succeeds(*args, **kwargs):
            argv = args[0]
            if "--abort" in argv:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=_abort_stalls_quit_succeeds):
            _cleanup_rebase_state(str(tmp_path))  # must not sys.exit


# ---------------------------------------------------------------------------
# sync_push error classification (#321)
# ---------------------------------------------------------------------------


class TestPushErrorClassification:
    @pytest.mark.parametrize("stderr", [
        "fatal: unable to access 'https://pi.local/vault.git/': "
        "Could not resolve host: pi.local",
        "ssh: connect to host pi.local port 22: Connection refused\n"
        "fatal: Could not read from remote repository.",
        "ssh: connect to host pi.local port 22: Operation timed out",
        "fatal: unable to access 'https://pi.local/vault.git/': "
        "Failed to connect to pi.local port 443: No route to host",
        "ssh: Could not resolve hostname schist-hub: "
        "Temporary failure in name resolution",
    ])
    def test_network_stderr_is_classified_unreachable(self, stderr):
        from schist.sync import _is_network_error

        assert _is_network_error(stderr) is True

    @pytest.mark.parametrize("stderr", [
        "fatal: invalid refspec ''",
        "fatal: You are not currently on a branch.",
        "error: src refspec main does not match any\n"
        "error: failed to push some refs to 'pi:vault.git'",
        "fatal: bad object HEAD",
    ])
    def test_local_fatal_stderr_is_not_unreachable(self, stderr):
        """#321: `fatal:` alone is not network evidence — git stamps it on
        purely local failures (bad refspec, detached HEAD, corruption)."""
        from schist.sync import _is_network_error

        assert _is_network_error(stderr) is False

    @patch("schist.sync.git_ops.push", return_value=(False, "fatal: invalid refspec ''"))
    @patch("schist.sync.git_ops.has_unpushed_commits", return_value=True)
    @patch("schist.sync.git_ops.has_uncommitted_changes", return_value=False)
    def test_local_push_failure_surfaces_real_stderr(
        self, mock_changes, mock_unpushed, mock_push, tmp_path, capsys
    ):
        """A local refspec error must NOT claim 'Hub unreachable' and must put
        git's actual stderr front and center."""
        from schist.sync import sync_push

        vault = _make_spoke(tmp_path)
        args = MagicMock()
        with pytest.raises(SystemExit):
            sync_push(args, vault, "db.sqlite")

        err = capsys.readouterr().err
        assert "unreachable" not in err.lower()
        assert "Push failed" in err
        assert "invalid refspec" in err

    @patch("schist.sync.git_ops.push",
           return_value=(False, "ssh: connect to host pi.local port 22: Connection refused"))
    @patch("schist.sync.git_ops.has_unpushed_commits", return_value=True)
    @patch("schist.sync.git_ops.has_uncommitted_changes", return_value=False)
    def test_ssh_transport_failure_still_reported_unreachable(
        self, mock_changes, mock_unpushed, mock_push, tmp_path, capsys
    ):
        from schist.sync import sync_push

        vault = _make_spoke(tmp_path)
        args = MagicMock()
        with pytest.raises(SystemExit):
            sync_push(args, vault, "db.sqlite")

        err = capsys.readouterr().err
        assert "unreachable" in err.lower()
        assert "Connection refused" in err


# ---------------------------------------------------------------------------
# .schist/ gitignore coverage (#309)
# ---------------------------------------------------------------------------


class TestSchistGitignore:
    @patch("schist.sync.git_ops.clone_shallow")
    @patch("schist.sync.git_ops.setup_sparse_checkout", return_value=(True, ""))
    @patch("schist.sync._rebuild_index")
    def test_spoke_exclude_covers_schist_dir(
        self, mock_ingest, mock_sparse, mock_clone, tmp_path
    ):
        """#309: the spoke's .git/info/exclude must ignore the whole .schist/
        runtime dir (SQLite index + WAL), not just spoke.yaml."""
        from schist.sync import init_spoke

        dest = str(tmp_path / "spoke")
        args = MagicMock(hub="git@pi:vault.git", scope="research/mario",
                         identity="cluster-mario")

        def create_at_arg(hub_url, dest_path, *a, **kw):
            Path(dest_path).mkdir(parents=True, exist_ok=True)
            (Path(dest_path) / ".git" / "info").mkdir(parents=True)
            return True, ""
        mock_clone.side_effect = create_at_arg

        init_spoke(args, dest, str(tmp_path / "db.sqlite"))

        lines = [
            line.strip()
            for line in (Path(dest) / ".git" / "info" / "exclude").read_text().splitlines()
        ]
        assert ".schist/" in lines
        assert ".schist/spoke.yaml" in lines

    def test_ensure_ignore_lines_idempotent(self, tmp_path):
        from schist.sync import _ensure_ignore_lines

        path = tmp_path / "exclude"
        _ensure_ignore_lines(path, [".schist/", ".schist/spoke.yaml"], comment="schist")
        first = path.read_text()

        _ensure_ignore_lines(path, [".schist/", ".schist/spoke.yaml"], comment="schist")

        assert path.read_text() == first
        assert first.splitlines().count(".schist/") == 1

    def test_ensure_ignore_lines_preserves_existing_content(self, tmp_path):
        from schist.sync import _ensure_ignore_lines

        path = tmp_path / "exclude"
        path.write_text("*.swp\n")
        _ensure_ignore_lines(path, [".schist/"])

        text = path.read_text()
        assert "*.swp" in text
        assert ".schist/" in text.splitlines()
