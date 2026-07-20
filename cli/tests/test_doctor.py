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
    check_mcp_vocab_alignment,
    check_node,
    check_post_commit_hook,
    check_python,
    check_root_gitignore,
    check_schist_yaml,
    check_spoke_identity_env,
    check_skill_tool_references,
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


class TestCheckRootGitignore:
    """Issue #362 — hubs seeded before #309 never gain the root .gitignore
    that excludes .schist/, so a broad `git add` in any working copy can
    commit the SQLite index. WARN (never FAIL): retrofitted spokes are
    already covered per-clone by .git/info/exclude (#354)."""

    def _vault(self, tmp_path: Path) -> Path:
        (tmp_path / ".git").mkdir()
        return tmp_path

    def test_no_path(self):
        r = check_root_gitignore(None)
        assert r.status == "SKIP"

    def test_not_a_git_repo(self, tmp_path):
        r = check_root_gitignore(str(tmp_path))
        assert r.status == "SKIP"

    def test_missing_gitignore_warns(self, tmp_path):
        vault = self._vault(tmp_path)
        r = check_root_gitignore(str(vault))
        assert r.status == "WARN"
        assert r.label == "Root .gitignore"
        # The warning must state the expected line.
        assert ".schist/" in r.message
        assert r.fix is not None
        assert ".schist/" in r.fix

    def test_gitignore_without_schist_line_warns(self, tmp_path):
        vault = self._vault(tmp_path)
        (vault / ".gitignore").write_text("*.pyc\nnode_modules/\n")
        r = check_root_gitignore(str(vault))
        assert r.status == "WARN"
        assert ".schist/" in r.message
        assert r.fix is not None

    def test_gitignore_with_schist_line_passes(self, tmp_path):
        vault = self._vault(tmp_path)
        # The exact content _build_seed_vault writes (sync.py).
        (vault / ".gitignore").write_text(".schist/\n")
        r = check_root_gitignore(str(vault))
        assert r.status == "PASS"

    @pytest.mark.parametrize("line", [".schist", "/.schist/", "/.schist",
                                      "  .schist/  "])
    def test_equivalent_ignore_forms_pass(self, tmp_path, line):
        vault = self._vault(tmp_path)
        (vault / ".gitignore").write_text(f"*.pyc\n{line}\n")
        r = check_root_gitignore(str(vault))
        assert r.status == "PASS"

    @pytest.mark.parametrize("line", [
        "# .schist/",       # comment, not a pattern
        "!.schist/",        # negation re-INCLUDES it
        ".schist/schist.db",  # narrower than the whole dir
        "notes/.schist/",   # different path
    ])
    def test_lookalike_lines_still_warn(self, tmp_path, line):
        vault = self._vault(tmp_path)
        (vault / ".gitignore").write_text(f"{line}\n")
        r = check_root_gitignore(str(vault))
        assert r.status == "WARN"


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


class TestCheckIngestAvailableStaleHandCopy:
    """Hand-provisioned `.schist/ingest.py` copies must be refreshed alongside
    slice B (#130 D3). A pre-contract copy re-stamps the old user_version
    after every commit while readers expect the new one — a silent
    rebuild-on-every-read ping-pong with no error anywhere. doctor is the
    only surface that can see it coming."""

    def _cli_schist_dir(self) -> Path:
        import schist.ingest

        return Path(schist.ingest.__file__).parent

    def _vault_with_copy(self, tmp_path: Path, *, with_sibling: bool,
                         script_text: str | None = None) -> Path:
        dot = tmp_path / ".schist"
        dot.mkdir(parents=True)
        src = self._cli_schist_dir()
        if script_text is None:
            (dot / "ingest.py").write_text((src / "ingest.py").read_text())
        else:
            (dot / "ingest.py").write_text(script_text)
        if with_sibling:
            (dot / "index_contract.py").write_text(
                (src / "index_contract.py").read_text()
            )
        return tmp_path

    def test_pass_when_copy_is_current(self, tmp_path):
        vault = self._vault_with_copy(tmp_path, with_sibling=True)
        with patch.dict(os.environ, {"SCHIST_INGEST_SCRIPT": ""}, clear=False):
            r = check_ingest_available(str(vault))
        assert r.status == "PASS", r.message

    def test_warn_when_sibling_index_contract_missing(self, tmp_path):
        vault = self._vault_with_copy(tmp_path, with_sibling=False)
        with patch.dict(os.environ, {"SCHIST_INGEST_SCRIPT": ""}, clear=False):
            r = check_ingest_available(str(vault))
        assert r.status == "WARN"
        assert "index_contract.py" in r.message
        assert r.fix is not None and "Refresh" in r.fix

    def test_warn_when_copy_stamps_hardcoded_version_one(self, tmp_path):
        pre_slice_b = (
            "#!/usr/bin/env python3\n"
            "def _ingest_into(conn):\n"
            "    conn.execute('PRAGMA user_version = 0')\n"
            "    conn.execute('PRAGMA user_version = 1')\n"
        )
        vault = self._vault_with_copy(
            tmp_path, with_sibling=True, script_text=pre_slice_b
        )
        with patch.dict(os.environ, {"SCHIST_INGEST_SCRIPT": ""}, clear=False):
            r = check_ingest_available(str(vault))
        assert r.status == "WARN"
        assert "user_version=1" in r.message

    def test_warn_when_sibling_index_contract_declares_stale_version(self, tmp_path):
        """#357: a sibling that EXISTS but declares an old schemaVersion is
        the same rebuild-loop trigger as a missing sibling — the hook copy
        restamps the old version after every commit while installed readers
        expect the new one. The presence check alone gave a false PASS."""
        vault = self._vault_with_copy(tmp_path, with_sibling=True)
        sibling = tmp_path / ".schist" / "index_contract.py"
        current = sibling.read_text()
        assert "'schemaVersion': 1," in current  # fixture guard: bump me on v2
        sibling.write_text(
            current.replace("'schemaVersion': 1,", "'schemaVersion': 999,")
        )
        with patch.dict(os.environ, {"SCHIST_INGEST_SCRIPT": ""}, clear=False):
            r = check_ingest_available(str(vault))
        assert r.status == "WARN"
        assert "declares schema v999" in r.message
        assert r.fix is not None and "Refresh" in r.fix

    def test_warn_when_sibling_declares_stale_version_double_quoted(self, tmp_path):
        """#380: the version-literal scan matched only single quotes, so a
        hand-edited sibling with a double-quoted literal sailed past the
        #357 check entirely."""
        vault = self._vault_with_copy(tmp_path, with_sibling=True)
        sibling = tmp_path / ".schist" / "index_contract.py"
        current = sibling.read_text()
        assert "'schemaVersion': 1," in current  # fixture guard: bump me on v2
        sibling.write_text(
            current.replace("'schemaVersion': 1,", '"schemaVersion": 999,')
        )
        with patch.dict(os.environ, {"SCHIST_INGEST_SCRIPT": ""}, clear=False):
            r = check_ingest_available(str(vault))
        assert r.status == "WARN"
        assert "declares schema v999" in r.message

    def test_warn_when_sibling_version_is_undeterminable(self, tmp_path):
        """#380: no version literal at all was treated as no-issue — a
        rewritten/truncated sibling passed while the runtime stamped
        something doctor never saw."""
        vault = self._vault_with_copy(tmp_path, with_sibling=True)
        sibling = tmp_path / ".schist" / "index_contract.py"
        current = sibling.read_text()
        sibling.write_text(
            current.replace("'schemaVersion': 1,", "'somethingElse': 1,")
        )
        with patch.dict(os.environ, {"SCHIST_INGEST_SCRIPT": ""}, clear=False):
            r = check_ingest_available(str(vault))
        assert r.status == "WARN"
        assert "cannot determine" in r.message

    def test_warn_when_354_window_copy_lacks_env_utils_sibling(self, tmp_path):
        """#369: the #354 revision of ingest.py imports env_utils with no
        inline fallback — without an env_utils.py sibling the post-commit
        hook dies with ModuleNotFoundError on every commit, and doctor said
        PASS throughout."""
        window_copy = (
            "#!/usr/bin/env python3\n"
            "try:\n"
            "    from .env_utils import env_flag\n"
            "except ImportError:\n"
            "    from env_utils import env_flag\n"
            "from index_contract import INDEX_SCHEMA_VERSION\n"
        )
        vault = self._vault_with_copy(
            tmp_path, with_sibling=True, script_text=window_copy
        )
        with patch.dict(os.environ, {"SCHIST_INGEST_SCRIPT": ""}, clear=False):
            r = check_ingest_available(str(vault))
        assert r.status == "WARN"
        assert "env_utils" in r.message
        assert r.fix is not None and "env_utils.py" in r.fix

    def test_pass_when_354_window_copy_has_env_utils_sibling(self, tmp_path):
        """The #354-window copy WITH the env_utils.py sibling works — no WARN."""
        window_copy = (
            "#!/usr/bin/env python3\n"
            "try:\n"
            "    from .env_utils import env_flag\n"
            "except ImportError:\n"
            "    from env_utils import env_flag\n"
            "from index_contract import INDEX_SCHEMA_VERSION\n"
        )
        vault = self._vault_with_copy(
            tmp_path, with_sibling=True, script_text=window_copy
        )
        src = self._cli_schist_dir()
        (tmp_path / ".schist" / "env_utils.py").write_text(
            (src / "env_utils.py").read_text()
        )
        with patch.dict(os.environ, {"SCHIST_INGEST_SCRIPT": ""}, clear=False):
            r = check_ingest_available(str(vault))
        assert r.status == "PASS", r.message

    def test_pass_when_current_copy_lacks_env_utils_sibling(self, tmp_path):
        """The CURRENT ingest.py defines the env_flag fallback inline, so a
        copy without the env_utils.py sibling is self-contained — doctor must
        not nag deployments that don't need the file."""
        vault = self._vault_with_copy(tmp_path, with_sibling=True)
        assert not (tmp_path / ".schist" / "env_utils.py").exists()
        with patch.dict(os.environ, {"SCHIST_INGEST_SCRIPT": ""}, clear=False):
            r = check_ingest_available(str(vault))
        assert r.status == "PASS", r.message


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


class TestCheckSpokeIdentityEnv:
    def test_no_path(self):
        r = check_spoke_identity_env(None)
        assert r.status == "SKIP"

    def test_not_spoke(self, tmp_path):
        r = check_spoke_identity_env(str(tmp_path))
        assert r.status == "SKIP"

    def test_fails_on_spoke_without_identity_env(self, tmp_path, monkeypatch):
        spoke_dir = tmp_path / ".schist"
        spoke_dir.mkdir()
        (spoke_dir / "spoke.yaml").write_text(yaml.dump({
            "hub": "file:///fake",
            "identity": "dragonfly",
            "scope": "global",
        }))
        monkeypatch.delenv("SCHIST_IDENTITY", raising=False)
        monkeypatch.delenv("GL_USER", raising=False)

        r = check_spoke_identity_env(str(tmp_path))

        assert r.status == "FAIL"
        assert "hub pushes will be rejected" in r.message
        assert r.fix and "SCHIST_IDENTITY" in r.fix and "GL_USER" in r.fix

    def test_passes_with_schist_identity(self, tmp_path, monkeypatch):
        spoke_dir = tmp_path / ".schist"
        spoke_dir.mkdir()
        (spoke_dir / "spoke.yaml").write_text(yaml.dump({
            "hub": "file:///fake",
            "identity": "dragonfly",
            "scope": "global",
        }))
        monkeypatch.setenv("SCHIST_IDENTITY", "dragonfly")
        monkeypatch.delenv("GL_USER", raising=False)

        r = check_spoke_identity_env(str(tmp_path))

        assert r.status == "PASS"
        assert "dragonfly" in r.message

    def test_empty_schist_identity_falls_through_to_gl_user(self, tmp_path, monkeypatch):
        spoke_dir = tmp_path / ".schist"
        spoke_dir.mkdir()
        (spoke_dir / "spoke.yaml").write_text(yaml.dump({
            "hub": "file:///fake",
            "identity": "gitolite-user",
            "scope": "global",
        }))
        monkeypatch.setenv("SCHIST_IDENTITY", "")
        monkeypatch.setenv("GL_USER", "gitolite-user")

        r = check_spoke_identity_env(str(tmp_path))

        assert r.status == "PASS"
        assert "gitolite-user" in r.message


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


class TestCheckIndexSchemaVersion:
    """#130 D3: the column-based alignment check above cannot see a pure
    schemaVersion bump (no new column the MCP reader SELECTs). This check
    compares the vault index's user_version, the installed CLI's
    INDEX_SCHEMA_VERSION, and the MCP dist's baked schemaVersion, and must
    name the direction-correct remedy — the runtime error from
    ensureSchemaCurrent claims doctor diagnoses the direction, so it has to."""

    def _write_dist_with_version(self, dist_dir: Path, version: int) -> None:
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "index.js").write_text("// stub\n")
        (dist_dir / "sqlite-reader.js").write_text(
            "export const INDEX_CONTRACT_FALLBACK = {\n"
            f"    schemaVersion: {version},\n"
            '    tables: ["docs"],\n'
            "};\n"
        )

    def _write_claude_json(self, tmp_path: Path, dist_dir: Path) -> None:
        (tmp_path / ".claude.json").write_text(json.dumps({
            "mcpServers": {"schist": {
                "command": "node", "args": [str(dist_dir / "index.js")],
            }}
        }))

    def _make_vault(self, tmp_path: Path, stamped: int) -> str:
        vault = tmp_path / "vault"
        (vault / ".schist").mkdir(parents=True)
        conn = sqlite3.connect(vault / ".schist" / "schist.db")
        conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY)")
        conn.execute(f"PRAGMA user_version = {stamped}")
        conn.commit()
        conn.close()
        return str(vault)

    def test_pass_when_all_current(self, tmp_path, monkeypatch):
        from schist.doctor import INDEX_SCHEMA_VERSION, check_index_schema_version

        vault = self._make_vault(tmp_path, INDEX_SCHEMA_VERSION)
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_dist_with_version(dist_dir, INDEX_SCHEMA_VERSION)
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_index_schema_version(vault)
        assert r.status == "PASS", r.message
        assert f"index v{INDEX_SCHEMA_VERSION}" in r.message
        assert f"MCP dist v{INDEX_SCHEMA_VERSION}" in r.message

    def test_pass_when_index_unstamped(self, tmp_path, monkeypatch):
        """user_version=0 is the in-flight/pre-marker state, not a skew."""
        from schist.doctor import check_index_schema_version

        vault = self._make_vault(tmp_path, 0)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_index_schema_version(vault)
        assert r.status == "PASS", r.message
        assert "unstamped" in r.message

    def test_warn_when_index_newer_than_cli(self, tmp_path, monkeypatch):
        """Index stamped by something newer → remedy is upgrading the CLI,
        NOT re-running ingest (which would silently downgrade the index)."""
        from schist.doctor import INDEX_SCHEMA_VERSION, check_index_schema_version

        vault = self._make_vault(tmp_path, INDEX_SCHEMA_VERSION + 1)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_index_schema_version(vault)
        assert r.status == "WARN"
        assert "NEWER" in r.message
        assert r.fix is not None and "uv tool install" in r.fix

    def test_warn_when_index_older_than_cli(self, tmp_path, monkeypatch):
        """Index predates the installed CLI's schema → remedy is a rebuild.
        (Requires INDEX_SCHEMA_VERSION >= 2 to be reachable, so pin the
        module constant doctor uses.)"""
        from schist.doctor import check_index_schema_version

        monkeypatch.setattr("schist.doctor.INDEX_SCHEMA_VERSION", 2)
        vault = self._make_vault(tmp_path, 1)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_index_schema_version(vault)
        assert r.status == "WARN"
        assert "stamped v1" in r.message
        assert r.fix is not None and "schist-ingest --vault" in r.fix

    def test_warn_when_mcp_dist_newer_than_cli(self, tmp_path, monkeypatch):
        """The runtime-error direction: newer mcp-server + older installed
        schist-ingest. Remedy is upgrading the CLI."""
        from schist.doctor import INDEX_SCHEMA_VERSION, check_index_schema_version

        vault = self._make_vault(tmp_path, INDEX_SCHEMA_VERSION)
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_dist_with_version(dist_dir, INDEX_SCHEMA_VERSION + 1)
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_index_schema_version(vault)
        assert r.status == "WARN"
        assert "MCP dist expects" in r.message
        assert r.fix is not None and "uv tool install" in r.fix

    def test_warn_when_mcp_dist_older_than_cli(self, tmp_path, monkeypatch):
        """Reverse skew: stale MCP dist. Remedy is rebuilding the dist —
        the direction the runtime error cannot tell the user about."""
        from schist.doctor import check_index_schema_version

        monkeypatch.setattr("schist.doctor.INDEX_SCHEMA_VERSION", 2)
        vault = self._make_vault(tmp_path, 2)
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_dist_with_version(dist_dir, 1)
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_index_schema_version(vault)
        assert r.status == "WARN"
        assert r.fix is not None and "npm run build" in r.fix

    def test_skip_without_index_db(self, tmp_path, monkeypatch):
        from schist.doctor import check_index_schema_version

        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_index_schema_version(str(vault))
        assert r.status == "SKIP"

    def test_skip_when_dist_predates_the_contract(self, tmp_path, monkeypatch):
        """Pre-slice-B dist has no INDEX_CONTRACT_FALLBACK — the dist leg is
        silently skipped, not misreported as skew."""
        from schist.doctor import INDEX_SCHEMA_VERSION, check_index_schema_version

        vault = self._make_vault(tmp_path, INDEX_SCHEMA_VERSION)
        dist_dir = tmp_path / "mcp" / "dist"
        dist_dir.mkdir(parents=True)
        (dist_dir / "index.js").write_text("// stub\n")
        (dist_dir / "sqlite-reader.js").write_text("// older MCP server\n")
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_index_schema_version(vault)
        assert r.status == "PASS", r.message
        assert "MCP dist" not in r.message


class TestCheckSkillToolReferences:
    """Guard against skills calling MCP tools the server no longer exposes —
    the request_capabilities (#72/#76) removal left dangling
    `mcp__schist__request_capabilities` calls in shared skills."""

    def _write_registry(self, dist_dir: Path, live: list[str], removed: list[str]) -> None:
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "index.js").write_text("// stub\n")
        tool_defs = "\n".join(
            f'  {{ name: "{t}", description: "x" }},' for t in live
        )
        removed_keys = "\n".join(f'  {t}: "gone",' for t in removed)
        (dist_dir / "tool-registry.js").write_text(
            f"export const tools = [\n{tool_defs}\n];\n"
            f"export const REMOVED_TOOLS = {{\n{removed_keys}\n}};\n"
        )

    def _write_claude_json(self, home: Path, dist_dir: Path) -> None:
        (home / ".claude.json").write_text(json.dumps({
            "mcpServers": {"schist": {
                "command": "node", "args": [str(dist_dir / "index.js")],
            }}
        }))

    def _write_skill(self, skills_dir: Path, name: str, tools: list[str]) -> None:
        d = skills_dir / name
        d.mkdir(parents=True, exist_ok=True)
        refs = "\n".join(f"  - mcp__schist__{t}" for t in tools)
        (d / "SKILL.md").write_text(f"---\nallowed-tools:\n{refs}\n---\n# {name}\n")

    def test_pass_when_all_refs_resolve(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        skills = vault / "shared" / "skills"
        self._write_skill(skills, "learn", ["add_memory", "search_memory"])
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_registry(dist_dir, ["add_memory", "search_memory"], [])
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_skill_tool_references(str(vault))
        assert r.status == "PASS", r.message

    def test_warn_on_removed_tool(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        skills = vault / "shared" / "skills"
        self._write_skill(skills, "learn", ["add_memory", "request_capabilities"])
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_registry(dist_dir, ["add_memory"], ["request_capabilities"])
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_skill_tool_references(str(vault))
        assert r.status == "WARN"
        assert "request_capabilities (removed" in r.message
        # The relative path names the offending skill, not a bare basename —
        # every skill's file is SKILL.md, so "learn/SKILL.md" must survive.
        assert "learn/SKILL.md" in r.message
        assert r.fix is not None and "restart" in r.fix.lower()

    def test_lists_each_skill_separately(self, tmp_path, monkeypatch):
        # Two skills reference the same removed tool. The bare basename would
        # collapse both to "SKILL.md"; the relative path keeps them distinct so
        # the user knows every file to fix.
        vault = tmp_path / "vault"
        skills = vault / "shared" / "skills"
        self._write_skill(skills, "learn", ["request_capabilities"])
        self._write_skill(skills, "handoff", ["request_capabilities"])
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_registry(dist_dir, ["add_memory"], ["request_capabilities"])
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_skill_tool_references(str(vault))
        assert r.status == "WARN"
        assert "learn/SKILL.md" in r.message
        assert "handoff/SKILL.md" in r.message

    def test_removed_parser_ignores_prose_colons(self, tmp_path):
        # A tombstone message containing "word:" (e.g. "unlock step: write")
        # must not be misread as a removed-tool key — only line-leading object
        # keys count.
        from schist.doctor import _extract_mcp_removed_tools
        dist_dir = tmp_path / "mcp" / "dist"
        dist_dir.mkdir(parents=True)
        (dist_dir / "tool-registry.js").write_text(
            "export const REMOVED_TOOLS = {\n"
            '    request_capabilities: "removed. there is no unlock step: write "\n'
            '        + "tools are callable directly. note: just call them.",\n'
            "};\n"
        )
        assert _extract_mcp_removed_tools(dist_dir) == {"request_capabilities"}

    def test_removed_parser_handles_brace_in_message(self, tmp_path):
        # A tombstone message containing a literal `}` must not truncate the
        # block early and drop later keys.
        from schist.doctor import _extract_mcp_removed_tools
        dist_dir = tmp_path / "mcp" / "dist"
        dist_dir.mkdir(parents=True)
        (dist_dir / "tool-registry.js").write_text(
            "export const REMOVED_TOOLS = {\n"
            '    old_one: "use the object } literal form instead",\n'
            '    old_two: "also gone",\n'
            "};\n"
        )
        assert _extract_mcp_removed_tools(dist_dir) == {"old_one", "old_two"}

    def test_tool_name_parser_ignores_name_in_description(self, tmp_path):
        # A `name: "x"` inside a description string or comment must not be
        # harvested as a live tool — that would mask a stale skill reference.
        from schist.doctor import _extract_mcp_tool_names
        dist_dir = tmp_path / "mcp" / "dist"
        dist_dir.mkdir(parents=True)
        (dist_dir / "tool-registry.js").write_text(
            '// name: "comment_ghost" should be ignored\n'
            "export const tools = [\n"
            '  { name: "real_tool", description: "mentions name: \\"prose_ghost\\" inline" },\n'
            "];\n"
        )
        names = _extract_mcp_tool_names(dist_dir)
        assert names == {"real_tool"}

    def test_scans_symlinked_skill_dir(self, tmp_path, monkeypatch):
        # Shared skills are commonly symlinked into the vault. The scan must
        # descend into symlinked directories or it misses exactly those skills.
        vault = tmp_path / "vault"
        skills = vault / "shared" / "skills"
        skills.mkdir(parents=True)
        real_skill = tmp_path / "real-skills" / "learn"
        real_skill.mkdir(parents=True)
        (real_skill / "SKILL.md").write_text(
            "---\nallowed-tools:\n  - mcp__schist__request_capabilities\n---\n"
        )
        (skills / "learn").symlink_to(real_skill, target_is_directory=True)
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_registry(dist_dir, ["add_memory"], ["request_capabilities"])
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_skill_tool_references(str(vault))
        assert r.status == "WARN"
        assert "request_capabilities (removed" in r.message
        assert "learn/SKILL.md" in r.message

    def test_warn_on_unknown_tool(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        skills = vault / "shared" / "skills"
        self._write_skill(skills, "weird", ["frobnicate"])
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_registry(dist_dir, ["add_memory"], [])
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_skill_tool_references(str(vault))
        assert r.status == "WARN"
        assert "frobnicate (unknown" in r.message

    def test_skip_when_no_skills_dir(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        vault.mkdir()
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_registry(dist_dir, ["add_memory"], [])
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_skill_tool_references(str(vault))
        assert r.status == "SKIP"
        assert "shared/skills" in r.message

    def test_skip_when_no_mcp_config(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        self._write_skill(vault / "shared" / "skills", "learn", ["add_memory"])
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_skill_tool_references(str(vault))
        assert r.status == "SKIP"
        assert "no MCP config" in r.message


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

    def test_skip_when_expected_dirs_unavailable(self, tmp_path, monkeypatch):
        import schist.doctor as doctor_mod
        from schist.doctor import check_hub_acl_drift
        hub = self._make_hub(tmp_path)

        def boom(_hub):
            raise FileNotFoundError("default.yaml missing")

        monkeypatch.setattr(doctor_mod, "_hub_expected_dirs", boom)
        r = check_hub_acl_drift(str(hub))
        assert r.status == "SKIP"


class TestDoctorHubWiring:
    def test_run_doctor_includes_hub_check_when_path_given(self, tmp_path, capsys):
        import shutil
        if shutil.which("git") is None:
            import pytest as _pytest
            _pytest.skip("git not available")
        from types import SimpleNamespace
        from schist.sync import init_hub
        from schist.doctor import run_doctor
        hub = tmp_path / "hub.git"
        init_hub(SimpleNamespace(name="v", participant=["alpha"]), str(hub))

        results = run_doctor(None, None, as_json=False, hub_path=str(hub))
        labels = [r.label for r in results]
        assert "Hub ACL drift" in labels

    def test_run_doctor_omits_hub_check_without_path(self):
        from schist.doctor import run_doctor
        results = run_doctor(None, None, as_json=False)
        labels = [r.label for r in results]
        assert "Hub ACL drift" not in labels


class TestCheckMcpVocabAlignment:
    """#414: the repo pins DEFAULT_CONNECTION_TYPES/DEFAULT_STATUSES against
    default.yaml with a test, but pip CLI and npm MCP server version
    independently — doctor must catch an installed pair that re-skewed
    (the #403 failure mode: MCP rejecting an edge type `schist link`
    accepts on a partial schist.yaml)."""

    @staticmethod
    def _cli_defaults():
        from schist.commands import _load_default_config
        cfg = _load_default_config()
        return list(cfg["connection_types"]), list(cfg["statuses"])

    def _write_dist_with_vocab(self, dist_dir, types, statuses):
        """Stub a tools.js with the named constants in tsc's emitted shape."""
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "index.js").write_text("// stub\n")
        t = ", ".join(f'"{x}"' for x in types)
        s = ", ".join(f'"{x}"' for x in statuses)
        (dist_dir / "tools.js").write_text(
            f"export const DEFAULT_CONNECTION_TYPES = [\n    {t},\n];\n"
            f"export const DEFAULT_STATUSES = [{s}];\n"
        )

    def _write_claude_json(self, tmp_path, dist_dir):
        import json as _json
        (tmp_path / ".claude.json").write_text(_json.dumps({
            "mcpServers": {"schist": {
                "command": "node", "args": [str(dist_dir / "index.js")],
            }}
        }))

    def test_pass_when_vocabularies_match(self, tmp_path, monkeypatch):
        types, statuses = self._cli_defaults()
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_dist_with_vocab(dist_dir, types, statuses)
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_vocab_alignment(None)
        assert r.status == "PASS", r.message
        assert "match" in r.message

    def test_order_difference_is_not_a_skew(self, tmp_path, monkeypatch):
        # Membership is what gates writes; a reordered baked list is
        # behaviorally identical and must not warn.
        types, statuses = self._cli_defaults()
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_dist_with_vocab(dist_dir, list(reversed(types)), statuses)
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_vocab_alignment(None)
        assert r.status == "PASS", r.message

    def test_warn_when_mcp_dist_misses_references(self, tmp_path, monkeypatch):
        """The exact #403 scenario: an older MCP dist baked the 7-item list
        without `references` while the CLI's default.yaml ships 8."""
        types, statuses = self._cli_defaults()
        assert "references" in types, "test prerequisite: default.yaml ships references"
        stale = [t for t in types if t != "references"]
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_dist_with_vocab(dist_dir, stale, statuses)
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_vocab_alignment(None)
        assert r.status == "WARN"
        assert "CLI-only references" in r.message
        assert r.fix is not None and "npm run build" in r.fix

    def test_warn_when_mcp_dist_has_extra_status(self, tmp_path, monkeypatch):
        types, statuses = self._cli_defaults()
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_dist_with_vocab(dist_dir, types, statuses + ["published"])
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_vocab_alignment(None)
        assert r.status == "WARN"
        assert "MCP-only published" in r.message

    def test_skip_when_no_mcp_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_vocab_alignment(None)
        assert r.status == "SKIP"
        assert "no MCP config" in r.message

    def test_skip_when_dist_predates_named_constants(self, tmp_path, monkeypatch):
        """A pre-#410 dist inlined the lists without names — SKIP, not a
        misreported skew."""
        dist_dir = tmp_path / "mcp" / "dist"
        dist_dir.mkdir(parents=True)
        (dist_dir / "index.js").write_text("// stub\n")
        (dist_dir / "tools.js").write_text(
            '// older MCP server\nconst x = ["extends", "supports"];\n'
        )
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_vocab_alignment(None)
        assert r.status == "SKIP"
        assert "not declared" in r.message

    def test_regexes_pin_against_real_tools_ts_source(self):
        """The extraction regexes must match the REAL tools.ts text (the `as
        const` source form) — a refactor of the literals there would
        otherwise silently downgrade the check to SKIP on the next build.
        Runs only in the repo layout (skipped for an installed package)."""
        from schist.doctor import _MCP_DEFAULT_VOCAB_RES, _VOCAB_ENTRY_STRING_RE

        tools_ts = Path(__file__).resolve().parents[2] / "mcp-server" / "src" / "tools.ts"
        if not tools_ts.exists():
            pytest.skip("mcp-server source not present (installed-package run)")
        text = tools_ts.read_text(encoding="utf-8")
        types, statuses = self._cli_defaults()
        m_t = _MCP_DEFAULT_VOCAB_RES["connection_types"].search(text)
        m_s = _MCP_DEFAULT_VOCAB_RES["statuses"].search(text)
        assert m_t and m_s, "named vocab constants not found in tools.ts"
        assert _VOCAB_ENTRY_STRING_RE.findall(m_t.group(1)) == types
        assert _VOCAB_ENTRY_STRING_RE.findall(m_s.group(1)) == statuses

    # -- /review findings on this check ------------------------------------

    def test_extraction_is_linear_on_adversarial_prose_mention(self):
        """/review finding: the sibling checks' `(?::[^=]*)?\\s*=` colon arm
        backtracks quadratically on an `=`-free tail after a prose mention
        ("DEFAULT_STATUSES:") — the memory-#152 O(n²) class. The vocab
        regexes carry no colon arm (the constants are never annotated), so
        this must fail fast. 1M-char probe with a huge CI margin."""
        import time
        from schist.doctor import _MCP_DEFAULT_VOCAB_RES

        text = "// see DEFAULT_STATUSES: keep in sync\n" + " " * 1_000_000
        t0 = time.perf_counter()
        assert _MCP_DEFAULT_VOCAB_RES["statuses"].search(text) is None
        assert time.perf_counter() - t0 < 5.0  # linear is ~ms; huge margin

    def test_prose_mention_cannot_bridge_to_a_wrong_array(self, tmp_path, monkeypatch):
        """/review finding: with a colon arm + DOTALL, a comment mentioning
        the constant name bridged across to the FIRST `= [...]` anywhere
        later, extracting a wrong array (false PASS or false WARN). Pinned:
        the comment mention must be inert and the real definition win."""
        from schist.doctor import _extract_mcp_default_vocab

        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        (dist_dir / "tools.js").write_text(
            '// NOTE on DEFAULT_CONNECTION_TYPES: keep in sync with default.yaml\n'
            'const decoy = ["wrong-entry"];\n'
            'export const DEFAULT_CONNECTION_TYPES = ["extends"];\n'
            'export const DEFAULT_STATUSES = ["draft"];\n'
        )
        vocab = _extract_mcp_default_vocab(dist_dir)
        assert vocab == {"connection_types": ["extends"], "statuses": ["draft"]}

    def test_extracts_entries_with_chars_outside_alphanum_hyphen(self, tmp_path):
        """#426: the write-time validator accepts any non-empty, whitespace-free
        token, so a vocab entry like "cites/chapter" or "applies-method-of.v2"
        is legal. The old [A-Za-z0-9_-]+ class captured NOTHING for such an
        entry, so it never entered the comparison set — a genuine CLI↔MCP skew
        on it read as a false PASS. The broadened [^'"\\s]+ class must now
        surface it."""
        from schist.doctor import _extract_mcp_default_vocab

        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        (dist_dir / "tools.js").write_text(
            'export const DEFAULT_CONNECTION_TYPES = '
            '["extends", "cites/chapter", "applies-method-of.v2"];\n'
            'export const DEFAULT_STATUSES = ["draft", "in:review"];\n'
        )
        vocab = _extract_mcp_default_vocab(dist_dir)
        assert vocab == {
            "connection_types": ["extends", "cites/chapter", "applies-method-of.v2"],
            "statuses": ["draft", "in:review"],
        }

    @pytest.mark.parametrize("breakage", ["raises", "non_mapping"])
    def test_corrupt_packaged_default_yaml_is_a_FAIL_not_a_crash(
            self, tmp_path, monkeypatch, breakage):
        """/review finding: run_doctor has no per-check exception shield, and
        _load_default_config raises on a corrupt (vs missing) default.yaml —
        the whole diagnostic died with a traceback exactly when the
        'reinstall schist' FAIL is the useful answer."""
        types, statuses = self._cli_defaults()
        dist_dir = tmp_path / "mcp" / "dist"
        self._write_dist_with_vocab(dist_dir, types, statuses)
        self._write_claude_json(tmp_path, dist_dir)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        if breakage == "raises":
            import yaml as _yaml

            def broken():
                raise _yaml.YAMLError("stray tab")
        else:
            def broken():
                return ["not", "a", "mapping"]
        monkeypatch.setattr("schist.commands._load_default_config", broken)
        r = check_mcp_vocab_alignment(None)
        assert r.status == "FAIL"
        assert "packaged default.yaml" in r.message
        assert r.fix is not None and "Reinstall" in r.fix
