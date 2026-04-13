"""Tests for spoke sync operations: init, pull, push."""

from __future__ import annotations

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

        # clone_shallow creates the directory
        def create_dir(*a, **kw):
            Path(dest).mkdir(parents=True, exist_ok=True)
            (Path(dest) / ".git" / "info").mkdir(parents=True)
            return True, ""
        mock_clone.side_effect = create_dir

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

    @patch("schist.sync.git_ops.pull_rebase", return_value=(False, "CONFLICT in research/mario/note.md"))
    def test_pull_conflict_aborts(self, mock_pull, tmp_path, capsys):
        from schist.sync import sync_pull

        vault = _make_spoke(tmp_path)
        args = MagicMock()
        with pytest.raises(SystemExit):
            sync_pull(args, vault, "db.sqlite")
        err = capsys.readouterr().err
        assert "pull failed" in err
        assert "conflict" in err.lower() or "re-clone" in err.lower()


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
