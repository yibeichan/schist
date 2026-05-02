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

        def create_dir(*a, **kw):
            Path(dest).mkdir(parents=True, exist_ok=True)
            (Path(dest) / ".git" / "info").mkdir(parents=True)
            return True, ""
        mock_clone.side_effect = create_dir

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


# ---------------------------------------------------------------------------
# _rebuild_index side-table preservation
# ---------------------------------------------------------------------------


def _init_vault_with_schema(
    tmp_path: Path, *, vault_yaml_body: str | None = None
) -> tuple[str, str]:
    """Set up a minimal vault + run a real ingest so schist.db exists with
    the current schema.sql applied. Returns (vault_path, db_path).

    If `vault_yaml_body` is provided, it's written to vault.yaml before the
    first rebuild — useful for exercising domain-taxonomy ingest.
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

    def test_domains_populated_from_vault_yaml(self, tmp_path):
        """Domains come from vault.yaml's top-level `domains:` list (the
        source of truth per schema/vault-yaml.md)."""
        import sqlite3

        vault_yaml = (
            "vault_version: 1\n"
            "name: test-vault\n"
            "scope_convention: subdirectory\n"
            "domains: [ai, security, ops]\n"
            "participants:\n"
            "  - name: tester\n"
            "    type: agent\n"
            "    default_scope: global\n"
            "access:\n"
            "  tester:\n"
            "    read: ['*']\n"
            "    write: ['*']\n"
        )
        _, db_path = _init_vault_with_schema(tmp_path, vault_yaml_body=vault_yaml)

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT slug, label, description, parent_slug FROM domains ORDER BY slug"
            ).fetchall()
        finally:
            conn.close()

        assert rows == [
            ("ai", "ai", None, None),
            ("ops", "ops", None, None),
            ("security", "security", None, None),
        ]

    def test_domains_reflect_vault_yaml_changes_across_rebuild(self, tmp_path):
        """Removing a domain from vault.yaml must remove it from SQLite on
        the next rebuild — the derived table follows the source of truth."""
        import sqlite3
        from schist.sync import _rebuild_index

        vault_yaml_v1 = (
            "vault_version: 1\n"
            "name: test\n"
            "scope_convention: subdirectory\n"
            "domains: [ai, ml, ops]\n"
            "participants: [{name: t, type: agent, default_scope: global}]\n"
            "access: {t: {read: ['*'], write: ['*']}}\n"
        )
        vault_path, db_path = _init_vault_with_schema(
            tmp_path, vault_yaml_body=vault_yaml_v1
        )

        # Remove `ml`, add `security`
        vault_yaml_v2 = vault_yaml_v1.replace(
            "domains: [ai, ml, ops]", "domains: [ai, ops, security]"
        )
        (Path(vault_path) / "vault.yaml").write_text(vault_yaml_v2)

        _rebuild_index(vault_path, db_path)

        conn = sqlite3.connect(db_path)
        try:
            slugs = [
                r[0] for r in conn.execute(
                    "SELECT slug FROM domains ORDER BY slug"
                ).fetchall()
            ]
        finally:
            conn.close()

        assert slugs == ["ai", "ops", "security"]

    def test_domains_empty_when_vault_yaml_missing(self, tmp_path):
        """Missing vault.yaml → domains table exists and is empty. No crash."""
        import sqlite3

        # _init_vault_with_schema with vault_yaml_body=None → no vault.yaml
        _, db_path = _init_vault_with_schema(tmp_path)

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_domains_empty_when_vault_yaml_lacks_domains_field(self, tmp_path):
        """vault.yaml without a top-level `domains:` field → empty table."""
        import sqlite3

        vault_yaml = (
            "vault_version: 1\n"
            "name: test\n"
            "scope_convention: subdirectory\n"
            "participants: [{name: t, type: agent, default_scope: global}]\n"
            "access: {t: {read: ['*'], write: ['*']}}\n"
        )
        _, db_path = _init_vault_with_schema(tmp_path, vault_yaml_body=vault_yaml)

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_domains_accept_dict_form(self, tmp_path):
        """Rich dict form for domains is accepted (future-proofing): each
        entry may be `{slug, label, description, parent_slug}`."""
        import sqlite3

        vault_yaml = (
            "vault_version: 1\n"
            "name: test\n"
            "scope_convention: subdirectory\n"
            "domains:\n"
            "  - slug: ai\n"
            "    label: Artificial Intelligence\n"
            "    description: AI research\n"
            "  - slug: ml\n"
            "    label: Machine Learning\n"
            "    parent_slug: ai\n"
            "participants: [{name: t, type: agent, default_scope: global}]\n"
            "access: {t: {read: ['*'], write: ['*']}}\n"
        )
        _, db_path = _init_vault_with_schema(tmp_path, vault_yaml_body=vault_yaml)

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT slug, label, description, parent_slug FROM domains ORDER BY slug"
            ).fetchall()
        finally:
            conn.close()

        assert rows == [
            ("ai", "Artificial Intelligence", "AI research", None),
            ("ml", "Machine Learning", None, "ai"),
        ]

    def test_domains_malformed_vault_yaml_does_not_crash(self, tmp_path):
        """Malformed YAML in vault.yaml → domains population skipped, ingest
        still succeeds (must not crash the post-commit hook)."""
        import sqlite3

        # Broken YAML — unclosed bracket
        _, db_path = _init_vault_with_schema(
            tmp_path, vault_yaml_body="domains: [ai, security\n"
        )

        # DB should exist and have an empty domains table
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0]
        finally:
            conn.close()
        assert count == 0

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
            # Both side tables should exist, both empty
            assert conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM concept_aliases").fetchone()[0] == 0
        finally:
            conn.close()

    def test_rebuild_handles_missing_side_table_in_backup(self, tmp_path):
        """Older DB format without `domains` or `concept_aliases` should not
        crash the rebuild — the missing tables should be skipped silently."""
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
        """Guard test: if schema.sql adds a column to domains or
        concept_aliases, the hardcoded column list in sync.py must be
        updated too. This test reads the actual schema from a fresh DB and
        compares against the hardcoded list.
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
