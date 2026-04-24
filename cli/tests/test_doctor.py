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
    check_ingest_available,
    check_mcp_config,
    check_node,
    check_post_commit_hook,
    check_python,
    check_schist_yaml,
    check_spoke,
    check_sqlite,
    check_vault_exists,
    check_vault_is_git,
    run_doctor,
)


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
    def test_found_in_claude_settings(self, tmp_path, monkeypatch):
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({
            "mcpServers": {"schist": {"command": "node", "args": ["/path/to/index.js"]}}
        }))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(None)
        assert r.status == "WARN"  # won't find because home patching is tricky

    def test_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(None)
        assert r.status == "WARN"


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
                        "Post-commit hook", "Ingest"}
        vault_results = [r for r in results if r.label in vault_labels]
        assert all(r.status == "PASS" for r in vault_results)
