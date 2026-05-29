"""Tests for schist doctor command."""

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from schist.doctor import (
    CheckResult,
    check_git,
    check_hooks_freshness,
    check_hooks_path,
    check_ingest_available,
    check_mcp_config,
    check_mcp_schema_alignment,
    check_node,
    check_post_commit_hook,
    check_python,
    check_schist_yaml,
    check_spoke,
    check_sqlite,
    check_uv,
    check_vault_exists,
    check_vault_is_git,
    run_doctor,
)
from schist.sync import HOOK_VERSION, POST_COMMIT_HOOK, PRE_COMMIT_HOOK


# ---------------------------------------------------------------------------
# Individual check tests
# ---------------------------------------------------------------------------


class TestCheckPython:
    def test_pass(self):
        r = check_python()
        assert r.status == "PASS"
        assert r.label == "Python"

    def test_fail(self):
        with patch("schist.doctor.sys") as mock_sys:
            mock_sys.version_info = (3, 11, 0)
            r = check_python()
            assert r.status == "FAIL"
            assert r.fix is not None


class TestCheckNode:
    def test_pass(self):
        r = check_node()
        # May pass or fail depending on test environment
        assert r.status in ("PASS", "FAIL")
        assert r.label == "Node.js"

    def test_not_found(self):
        with patch("shutil.which", return_value=None):
            r = check_node()
            assert r.status == "FAIL"

    def test_old_version(self):
        with patch("shutil.which", return_value="/usr/bin/node"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="v18.0.0\n"
                )
                r = check_node()
                assert r.status == "FAIL"


class TestCheckUv:
    def test_pass_when_installed(self):
        with patch("shutil.which", return_value="/usr/local/bin/uv"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="uv 0.5.24\n"
                )
                r = check_uv()
                assert r.status == "PASS"
                assert r.label == "uv"
                assert "0.5.24" in r.message

    def test_warn_when_missing(self):
        with patch("shutil.which", return_value=None):
            r = check_uv()
            assert r.status == "WARN"
            assert r.label == "uv"
            assert "not found" in r.message
            # Recommendation should mention uv install + pip fallback so users
            # know they can keep going either way.
            assert r.fix and "astral.sh" in r.fix and "pip" in r.fix

    def test_warn_when_subprocess_raises(self):
        # uv binary present but `uv --version` throws (timeout, broken install,
        # permission error, etc.) — surface a WARN with an install pointer
        # rather than crashing the whole doctor run.
        with patch("shutil.which", return_value="/usr/local/bin/uv"):
            with patch("subprocess.run", side_effect=OSError("permission denied")):
                r = check_uv()
                assert r.status == "WARN"
                assert r.label == "uv"
                assert "error" in r.message
                assert r.fix and "astral.sh" in r.fix


class TestCheckGit:
    def test_pass(self):
        r = check_git()
        assert r.status in ("PASS", "FAIL")

    def test_not_found(self):
        with patch("shutil.which", return_value=None):
            r = check_git()
            assert r.status == "FAIL"

    def test_old_version(self):
        with patch("shutil.which", return_value="/usr/bin/git"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="git version 2.17.1\n"
                )
                r = check_git()
                assert r.status == "FAIL"


class TestCheckVaultExists:
    def test_no_path(self):
        r = check_vault_exists(None)
        assert r.status == "SKIP"

    def test_exists(self, tmp_path):
        r = check_vault_exists(str(tmp_path))
        assert r.status == "PASS"

    def test_not_exists(self):
        r = check_vault_exists("/nonexistent/path/vault")
        assert r.status == "FAIL"


class TestCheckVaultIsGit:
    def test_no_path(self):
        r = check_vault_is_git(None)
        assert r.status == "SKIP"

    def test_is_git(self, tmp_path):
        (tmp_path / ".git").mkdir()
        r = check_vault_is_git(str(tmp_path))
        assert r.status == "PASS"

    def test_not_git(self, tmp_path):
        r = check_vault_is_git(str(tmp_path))
        assert r.status == "FAIL"


class TestCheckSchistYaml:
    def test_no_path(self):
        r = check_schist_yaml(None)
        assert r.status == "SKIP"

    def test_valid(self, tmp_path):
        (tmp_path / "schist.yaml").write_text(yaml.dump({"name": "test"}))
        r = check_schist_yaml(str(tmp_path))
        assert r.status == "PASS"

    def test_missing(self, tmp_path):
        r = check_schist_yaml(str(tmp_path))
        assert r.status == "FAIL"

    def test_invalid(self, tmp_path):
        (tmp_path / "schist.yaml").write_text("{{invalid")
        r = check_schist_yaml(str(tmp_path))
        assert r.status == "FAIL"


class TestCheckSqlite:
    def test_no_path(self):
        r = check_sqlite(None, None)
        assert r.status == "SKIP"

    def test_valid(self, tmp_path):
        db = tmp_path / ".schist" / "schist.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE docs (id TEXT)")
        conn.execute("INSERT INTO docs VALUES ('x')")
        conn.execute("CREATE TABLE concepts (id TEXT)")
        conn.execute("CREATE TABLE edges (source TEXT, target TEXT)")
        conn.commit()
        conn.close()
        r = check_sqlite(str(tmp_path), str(db))
        assert r.status == "PASS"
        assert "1 docs" in r.message

    def test_missing_db(self, tmp_path):
        r = check_sqlite(str(tmp_path), None)
        assert r.status == "FAIL"


class TestCheckPostCommitHook:
    def test_no_path(self):
        r = check_post_commit_hook(None)
        assert r.status == "SKIP"

    def test_installed(self, tmp_path):
        hooks = tmp_path / ".git" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "post-commit").write_text("#!/bin/sh\n")
        r = check_post_commit_hook(str(tmp_path))
        assert r.status == "PASS"

    def test_missing(self, tmp_path):
        (tmp_path / ".git").mkdir()
        r = check_post_commit_hook(str(tmp_path))
        assert r.status == "FAIL"


class TestCheckHooksFreshness:
    """Issue #103 — detect spokes still running an older hook template so
    fixes to the secret regex actually reach existing installations."""

    def _install_hook(self, vault: Path, name: str, body: str) -> None:
        hooks = vault / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / name).write_text(body)

    def test_no_path(self):
        r = check_hooks_freshness(None)
        assert r.status == "SKIP"

    def test_not_a_git_repo(self, tmp_path):
        r = check_hooks_freshness(str(tmp_path))
        assert r.status == "SKIP"

    def test_current_versions_pass(self, tmp_path):
        (tmp_path / ".git").mkdir()
        self._install_hook(tmp_path, "pre-commit", PRE_COMMIT_HOOK)
        self._install_hook(tmp_path, "post-commit", POST_COMMIT_HOOK)
        r = check_hooks_freshness(str(tmp_path))
        assert r.status == "PASS"
        assert f"v{HOOK_VERSION}" in r.message

    def test_legacy_unversioned_hook_warns(self, tmp_path):
        """A spoke init'd before HOOK_VERSION was introduced has no marker —
        must surface as stale so the user knows to reinstall."""
        (tmp_path / ".git").mkdir()
        self._install_hook(tmp_path, "pre-commit",
                           "#!/bin/sh\n# legacy hook with no version marker\nexit 0\n")
        self._install_hook(tmp_path, "post-commit", POST_COMMIT_HOOK)
        r = check_hooks_freshness(str(tmp_path))
        assert r.status == "WARN"
        assert "legacy" in r.message
        assert r.fix is not None
        assert "hooks reinstall" in r.fix

    def test_old_versioned_hook_warns(self, tmp_path):
        (tmp_path / ".git").mkdir()
        self._install_hook(tmp_path, "pre-commit",
                           "#!/bin/sh\n# schist-hook-version: 1\nexit 0\n")
        self._install_hook(tmp_path, "post-commit", POST_COMMIT_HOOK)
        r = check_hooks_freshness(str(tmp_path))
        assert r.status == "WARN"
        assert "v1" in r.message
        assert f"v{HOOK_VERSION}" in r.message

    def test_pinned_marker_silences_warning(self, tmp_path):
        """User who customized their hook can opt out with `pinned`."""
        (tmp_path / ".git").mkdir()
        self._install_hook(tmp_path, "pre-commit",
                           "#!/bin/sh\n# schist-hook-version: pinned\n# my custom patterns\nexit 0\n")
        self._install_hook(tmp_path, "post-commit", POST_COMMIT_HOOK)
        r = check_hooks_freshness(str(tmp_path))
        assert r.status == "PASS"
        assert "pinned" in r.message


class TestCheckHooksPath:
    """Issue #40 — warn when core.hooksPath redirects git away from
    .git/hooks/ so schist's installed hooks are silently bypassed."""

    def test_no_path(self):
        r = check_hooks_path(None)
        assert r.status == "SKIP"

    def test_unset_returns_pass(self, tmp_path):
        # Init a fresh repo with no core.hooksPath set.
        subprocess.run(["git", "init", str(tmp_path)], check=True,
                       capture_output=True)
        r = check_hooks_path(str(tmp_path))
        assert r.status == "PASS"
        assert r.label == "Hooks path"

    def test_set_returns_warn(self, tmp_path):
        """When core.hooksPath is set to a non-default value, the schist
        hooks at .git/hooks/ are bypassed — warn loudly."""
        subprocess.run(["git", "init", str(tmp_path)], check=True,
                       capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.hooksPath", "/tmp/elsewhere"],
            check=True, capture_output=True,
        )
        r = check_hooks_path(str(tmp_path))
        assert r.status == "WARN"
        assert "core.hooksPath" in r.message
        assert "/tmp/elsewhere" in r.message
        assert r.fix is not None

    def test_not_a_git_repo(self, tmp_path):
        """If the vault path isn't a git repo at all, SKIP (other doctor
        checks will FAIL appropriately for the missing .git/)."""
        r = check_hooks_path(str(tmp_path))
        assert r.status == "SKIP"


class TestCheckIngestAvailable:
    def test_no_path(self):
        r = check_ingest_available(None)
        assert r.status == "SKIP"

    def test_on_path(self, tmp_path):
        with patch("shutil.which", return_value="/usr/bin/schist-ingest"):
            r = check_ingest_available(str(tmp_path))
            assert r.status == "PASS"

    def test_env_var(self, tmp_path):
        script = tmp_path / "my-ingest.py"
        script.write_text("#!/usr/bin/env python3\n")
        with patch.dict(os.environ, {"SCHIST_INGEST_SCRIPT": str(script)}):
            r = check_ingest_available(str(tmp_path))
            assert r.status == "PASS"

    def test_not_found(self, tmp_path):
        with patch("shutil.which", return_value=None):
            with patch.dict(os.environ, {"SCHIST_INGEST_SCRIPT": ""}, clear=False):
                r = check_ingest_available(str(tmp_path))
                assert r.status == "FAIL"


class TestCheckSpoke:
    def test_no_path(self):
        r = check_spoke(None)
        assert r.status == "SKIP"

    def test_not_spoke(self, tmp_path):
        r = check_spoke(str(tmp_path))
        assert r.status == "SKIP"

    def test_valid_spoke(self, tmp_path):
        spoke_dir = tmp_path / ".schist"
        spoke_dir.mkdir()
        spoke_dir.mkdir(parents=True, exist_ok=True)
        (spoke_dir / "spoke.yaml").write_text(yaml.dump({
            "hub": "https://github.com/test/repo.git",
            "identity": "test",
            "scope": "research/test",
        }))
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=""
            )
            r = check_spoke(str(tmp_path))
            assert r.status == "PASS"

    def test_hub_timeout(self, tmp_path):
        spoke_dir = tmp_path / ".schist"
        spoke_dir.mkdir()
        (spoke_dir / "spoke.yaml").write_text(yaml.dump({
            "hub": "https://github.com/test/repo.git",
            "identity": "test",
            "scope": "research/test",
        }))
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10)):
            r = check_spoke(str(tmp_path))
            assert r.status == "WARN"


class TestCheckMcpConfig:
    def test_found_in_claude_code_user_config(self, tmp_path, monkeypatch):
        """Claude Code (the active product) stores user-scope MCP servers in
        ~/.claude.json — distinct from Claude Desktop's ~/.claude/settings.json.
        """
        fake_mcp = tmp_path / "fake-mcp" / "dist" / "index.js"
        fake_mcp.parent.mkdir(parents=True)
        fake_mcp.write_text("// stub\n")
        (tmp_path / ".claude.json").write_text(json.dumps({
            "mcpServers": {"schist": {"command": "node", "args": [str(fake_mcp)]}}
        }))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        # Patch auto-detect so sub-check 3 doesn't fire for a stale fake path.
        with patch("schist.doctor._auto_detect_mcp_path", return_value=None):
            r = check_mcp_config(None)
        assert r.status == "PASS"
        assert ".claude.json" in r.message

    def test_found_in_claude_desktop_settings(self, tmp_path, monkeypatch):
        fake_mcp = tmp_path / "fake-mcp" / "dist" / "index.js"
        fake_mcp.parent.mkdir(parents=True)
        fake_mcp.write_text("// stub\n")
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").write_text(json.dumps({
            "mcpServers": {"schist": {"command": "node", "args": [str(fake_mcp)]}}
        }))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        # Patch auto-detect so sub-check 3 doesn't fire for a stale fake path.
        with patch("schist.doctor._auto_detect_mcp_path", return_value=None):
            r = check_mcp_config(None)
        assert r.status == "PASS"

    def test_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(None)
        assert r.status == "WARN"

    def test_args0_missing_returns_warn(self, tmp_path, monkeypatch):
        """Issue #43 sub-check 1: WARN when args[0] doesn't exist on disk."""
        (tmp_path / ".claude.json").write_text(json.dumps({
            "mcpServers": {"schist": {
                "command": "node",
                "args": [str(tmp_path / "nope" / "missing.js")],
            }}
        }))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(None)
        assert r.status == "WARN"
        assert "does not exist" in r.message

    def test_vault_path_env_mismatch_returns_warn(self, tmp_path, monkeypatch):
        """Issue #43 sub-check 2: WARN when entry's SCHIST_VAULT_PATH env
        differs from the current vault_path passed to the doctor."""
        fake_mcp = tmp_path / "fake-mcp" / "dist" / "index.js"
        fake_mcp.parent.mkdir(parents=True)
        fake_mcp.write_text("// stub\n")
        current_vault = tmp_path / "current-vault"
        current_vault.mkdir()
        wrong_vault = tmp_path / "old-vault"
        wrong_vault.mkdir()
        (tmp_path / ".claude.json").write_text(json.dumps({
            "mcpServers": {"schist": {
                "command": "node",
                "args": [str(fake_mcp)],
                "env": {"SCHIST_VAULT_PATH": str(wrong_vault)},
            }}
        }))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(str(current_vault))
        assert r.status == "WARN"
        assert "SCHIST_VAULT_PATH" in r.message

    def test_aggregates_multiple_warnings(self, tmp_path, monkeypatch):
        """Multiple sub-check failures aggregate into ONE WARN result whose
        message lists each failure (joined with '; ')."""
        # Both args[0] missing AND env mismatch in the same entry.
        current_vault = tmp_path / "current"
        current_vault.mkdir()
        (tmp_path / ".claude.json").write_text(json.dumps({
            "mcpServers": {"schist": {
                "command": "node",
                "args": [str(tmp_path / "nope.js")],
                "env": {"SCHIST_VAULT_PATH": str(tmp_path / "old")},
            }}
        }))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(str(current_vault))
        assert r.status == "WARN"
        assert "does not exist" in r.message
        assert "SCHIST_VAULT_PATH" in r.message
        assert "; " in r.message  # aggregated, not just one reason

    def test_auto_detect_drift_returns_warn(self, tmp_path, monkeypatch):
        """Issue #43 sub-check 3: WARN when the entry's args[0] differs from
        the auto-detected current mcp-server/dist/index.js."""
        # Set up a real-on-disk args[0] (so sub-check 1 passes)
        entry_mcp = tmp_path / "old-checkout" / "dist" / "index.js"
        entry_mcp.parent.mkdir(parents=True)
        entry_mcp.write_text("// stale\n")

        # Patch the auto-detect helper to return a different path.
        # The enhanced check_mcp_config calls a private helper for this; the
        # test patches that helper to return a synthetic 'current' path.
        # If implementation uses an inline auto-detect, the patch target is
        # `schist.doctor._auto_detect_mcp_path` (extract one in Task 5).
        with patch("schist.doctor._auto_detect_mcp_path",
                   return_value=str(tmp_path / "fresh-checkout" / "dist" / "index.js")):
            (tmp_path / ".claude.json").write_text(json.dumps({
                "mcpServers": {"schist": {
                    "command": "node",
                    "args": [str(entry_mcp)],
                }}
            }))
            monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
            r = check_mcp_config(None)
            assert r.status == "WARN"
            assert "auto-detected" in r.message or "differs" in r.message


class TestCheckMcpSchemaAlignment:
    """Guard against the OLD-MCP / NEW-ingest skew that surfaces as the
    misleading 'schist-ingest is older' error from ensureSchemaCurrent
    (mcp-server/src/sqlite-reader.ts:140-146).
    """

    def _write_dist_with_columns(self, dist_dir: Path, cols: list[str]) -> None:
        """Stub a `sqlite-reader.js` containing a REQUIRED_DOCS_COLUMNS
        Set literal matching the regex in doctor.py."""
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "index.js").write_text("// stub\n")
        col_strs = ", ".join(f'"{c}"' for c in cols)
        (dist_dir / "sqlite-reader.js").write_text(
            f"const REQUIRED_DOCS_COLUMNS = new Set([\n  {col_strs},\n]);\n"
        )

    def _write_claude_json(self, tmp_path: Path, dist_dir: Path) -> None:
        (tmp_path / ".claude.json").write_text(json.dumps({
            "mcpServers": {"schist": {
                "command": "node", "args": [str(dist_dir / "index.js")],
            }}
        }))

    def test_pass_when_sets_match(self, tmp_path, monkeypatch):
        """In-sync MCP dist + schema.sql → PASS."""
        # Canonical columns are derived from the bundled schema.sql; use
        # _canonical_docs_columns to pin the test to whatever schist ships.
        from schist.doctor import _canonical_docs_columns
        canonical = _canonical_docs_columns()
        assert canonical is not None, "test prerequisite: schema.sql must load"
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_dist_with_columns(dist_dir, sorted(canonical))
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_schema_alignment(None)
        assert r.status == "PASS", r.message
        assert "in sync" in r.message

    def test_warn_when_mcp_expects_retired_column(self, tmp_path, monkeypatch):
        """The #146 scenario: MCP dist still lists `domain` after the
        ingest schema dropped it. doctor must WARN with a 'rebuild MCP'
        fix — NOT 'reinstall schist-ingest' (the misleading runtime error)."""
        from schist.doctor import _canonical_docs_columns
        canonical = _canonical_docs_columns()
        assert canonical is not None
        stale_cols = sorted(canonical | {"domain"})
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_dist_with_columns(dist_dir, stale_cols)
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_schema_alignment(None)
        assert r.status == "WARN"
        assert "retired columns: domain" in r.message
        assert r.fix is not None and "npm run build" in r.fix

    def test_pass_when_mcp_required_is_proper_subset(self, tmp_path, monkeypatch):
        """Canonical-only columns (e.g. `created_at`, `updated_at`) that MCP
        doesn't read aren't a skew. The check must NOT warn on the reverse
        direction — MCP only declares the columns it SELECTs, by design."""
        from schist.doctor import _canonical_docs_columns
        canonical = _canonical_docs_columns()
        assert canonical is not None
        # Drop a column MCP doesn't need to read — pick a timestamp that
        # really is in the canonical set but absent from REQUIRED_DOCS_COLUMNS.
        assert {"created_at", "updated_at"} <= canonical, (
            "test prerequisite: timestamp columns must be in canonical schema"
        )
        cols_mcp_reads = sorted(canonical - {"created_at", "updated_at"})
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_dist_with_columns(dist_dir, cols_mcp_reads)
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_schema_alignment(None)
        assert r.status == "PASS", r.message
        assert "in sync" in r.message

    def test_skip_when_no_mcp_config(self, tmp_path, monkeypatch):
        """No MCP entry configured → SKIP, not FAIL (check_mcp_config
        already surfaces the missing-entry case)."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_schema_alignment(None)
        assert r.status == "SKIP"
        assert "no MCP config" in r.message

    def test_skip_when_dist_predates_drift_detection(self, tmp_path, monkeypatch):
        """Pre-#145 MCP dist doesn't declare REQUIRED_DOCS_COLUMNS — SKIP
        instead of misreporting the unparseable file as a skew."""
        dist_dir = tmp_path / "mcp" / "dist"
        dist_dir.mkdir(parents=True)
        (dist_dir / "index.js").write_text("// stub\n")
        (dist_dir / "sqlite-reader.js").write_text(
            "// older MCP server — no REQUIRED_DOCS_COLUMNS yet\n"
        )
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_schema_alignment(None)
        assert r.status == "SKIP"
        assert "REQUIRED_DOCS_COLUMNS not declared" in r.message


# ---------------------------------------------------------------------------
# Integration: run_doctor
# ---------------------------------------------------------------------------


class TestRunDoctor:
    def test_no_vault_text(self, capsys):
        results = run_doctor(None, None, as_json=False)
        assert any(r.status == "SKIP" for r in results)
        captured = capsys.readouterr()
        assert "[PASS] Python:" in captured.out

    def test_no_vault_json(self, capsys):
        results = run_doctor(None, None, as_json=True)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert any(d["label"] == "Python" for d in data)

    def test_full_vault(self, tmp_path, capsys):
        # Set up a minimal valid vault
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "hooks").mkdir()
        (tmp_path / ".git" / "hooks" / "post-commit").write_text("#!/bin/sh\n")
        (tmp_path / "schist.yaml").write_text(yaml.dump({"name": "test"}))

        db = tmp_path / ".schist" / "schist.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE docs (id TEXT)")
        conn.execute("CREATE TABLE concepts (id TEXT)")
        conn.execute("CREATE TABLE edges (source TEXT, target TEXT)")
        conn.commit()
        conn.close()

        with patch("shutil.which", return_value="/usr/bin/schist-ingest"):
            results = run_doctor(str(tmp_path), str(db), as_json=False)

        captured = capsys.readouterr()
        assert "[PASS] Python:" in captured.out
        assert "[PASS] Vault:" in captured.out
        assert "[PASS] schist.yaml:" in captured.out
        assert "[PASS] SQLite:" in captured.out
        # Vault-specific checks should all pass
        vault_labels = {"Vault", "Git repo", "schist.yaml", "SQLite",
                        "Post-commit hook", "Hooks path", "Ingest"}
        vault_results = [r for r in results if r.label in vault_labels]
        assert all(r.status == "PASS" for r in vault_results)


# ---------------------------------------------------------------------------
# check_spoke_acl_drift tests
# ---------------------------------------------------------------------------

from schist.doctor import check_spoke_acl_drift  # noqa: E402


def _write_vault(tmp_path: Path, schist_yaml: str, vault_yaml: str | None,
                 spoke_yaml: str | None) -> Path:
    (tmp_path / "schist.yaml").write_text(schist_yaml)
    if vault_yaml is not None:
        (tmp_path / "vault.yaml").write_text(vault_yaml)
    if spoke_yaml is not None:
        (tmp_path / ".schist").mkdir(exist_ok=True)
        (tmp_path / ".schist" / "spoke.yaml").write_text(spoke_yaml)
    return tmp_path


def test_drift_present_warns(tmp_path: Path) -> None:
    _write_vault(
        tmp_path,
        schist_yaml="directories:\n  notes: notes/\n  papers: papers/\n  logs: logs/\n",
        vault_yaml="""\
vault_version: 1
name: test
scope_convention: flat
participants:
  - name: orcd
    type: spoke
    default_scope: global
access:
  orcd:
    read: ["*"]
    write: [notes, papers]
""",
        spoke_yaml="hub: file:///fake\nidentity: orcd\nscope: global\n",
    )
    result = check_spoke_acl_drift(str(tmp_path))
    assert result.status == "WARN"
    assert "logs" in result.message
    assert "orcd" in result.message


def test_no_drift_passes(tmp_path: Path) -> None:
    _write_vault(
        tmp_path,
        schist_yaml="directories:\n  notes: notes/\n",
        vault_yaml="""\
vault_version: 1
name: test
scope_convention: flat
participants:
  - name: orcd
    type: spoke
    default_scope: global
access:
  orcd:
    read: ["*"]
    write: [notes]
""",
        spoke_yaml="hub: file:///fake\nidentity: orcd\nscope: global\n",
    )
    result = check_spoke_acl_drift(str(tmp_path))
    assert result.status == "PASS"


def test_no_vault_yaml_skips(tmp_path: Path) -> None:
    _write_vault(
        tmp_path,
        schist_yaml="directories:\n  notes: notes/\n",
        vault_yaml=None,
        spoke_yaml="hub: file:///fake\nidentity: orcd\nscope: global\n",
    )
    result = check_spoke_acl_drift(str(tmp_path))
    assert result.status == "SKIP"


def test_not_a_spoke_skips(tmp_path: Path) -> None:
    _write_vault(
        tmp_path,
        schist_yaml="directories:\n  notes: notes/\n",
        vault_yaml="""\
vault_version: 1
name: standalone
scope_convention: flat
participants:
  - name: local
    type: agent
    default_scope: global
access:
  local:
    read: ["*"]
    write: ["*"]
""",
        spoke_yaml=None,  # not a spoke
    )
    result = check_spoke_acl_drift(str(tmp_path))
    assert result.status == "SKIP"


def test_wildcard_grant_passes(tmp_path: Path) -> None:
    _write_vault(
        tmp_path,
        schist_yaml="directories:\n  notes: notes/\n  logs: logs/\n",
        vault_yaml="""\
vault_version: 1
name: test
scope_convention: flat
participants:
  - name: admin
    type: spoke
    default_scope: global
access:
  admin:
    read: ["*"]
    write: ["*"]
""",
        spoke_yaml="hub: file:///fake\nidentity: admin\nscope: global\n",
    )
    result = check_spoke_acl_drift(str(tmp_path))
    assert result.status == "PASS"


class TestHubAclDrift:
    def _make_hub(self, tmp_path):
        import shutil
        if shutil.which("git") is None:
            import pytest as _pytest
            _pytest.skip("git not available")
        from types import SimpleNamespace
        from schist.sync import init_hub
        hub = tmp_path / "hub.git"
        init_hub(SimpleNamespace(name="v", participant=["alpha", "beta"]), str(hub))
        return hub

    def test_skip_without_hub_path(self):
        from schist.doctor import check_hub_acl_drift
        r = check_hub_acl_drift(None)
        assert r.status == "SKIP"

    def test_warns_on_dir_granted_to_nobody(self, tmp_path):
        # 'decisions' is in default.yaml expected dirs and seeded to both;
        # revoke from BOTH -> signal (a) fires.
        from schist import hub_admin
        from schist.doctor import check_hub_acl_drift
        hub = self._make_hub(tmp_path)
        hub_admin.apply_mutation(hub, lambda d: hub_admin.revoke_write(d, "alpha", "decisions"), "m")
        hub_admin.apply_mutation(hub, lambda d: hub_admin.revoke_write(d, "beta", "decisions"), "m")
        r = check_hub_acl_drift(str(hub))
        assert r.status == "WARN"
        assert "decisions" in r.message

    def test_warns_on_cross_participant_inconsistency(self, tmp_path):
        # Grant 'logs' to alpha only -> signal (b). 'logs' is infra (excluded
        # from expected dirs) so signal (a) won't fire for it.
        from schist import hub_admin
        from schist.doctor import check_hub_acl_drift
        hub = self._make_hub(tmp_path)
        hub_admin.apply_mutation(hub, lambda d: hub_admin.grant_write(d, "alpha", "logs"), "m")
        r = check_hub_acl_drift(str(hub))
        assert r.status == "WARN"
        assert "logs" in r.message

    def test_pass_when_consistent_and_covered(self, tmp_path):
        from schist.doctor import check_hub_acl_drift
        hub = self._make_hub(tmp_path)
        r = check_hub_acl_drift(str(hub))
        # Seed grants all 6 content dirs to both; infra dirs (logs/projects)
        # excluded from expected -> no drift.
        assert r.status == "PASS"
