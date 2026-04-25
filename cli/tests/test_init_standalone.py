"""Tests for `schist init` standalone mode and the unified init dispatch."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def _has_git() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not available")


REPO_ROOT = Path(__file__).resolve().parents[2]


def _args(**kwargs) -> SimpleNamespace:
    """Build an argparse-like args namespace with standalone-init defaults."""
    defaults = {
        "path": None,
        "name": None,
        "identity": None,
        "spoke": False,
        "hub": None,
        "hub_path": None,
        "scope": None,
        "participant": None,
        "vault": None,
        "db": None,
        "print_mcp_config": False,
        "mcp_format": "claude",
        "mcp_server_path": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestInitStandalone:
    def test_creates_vault_at_explicit_path(self, tmp_path):
        from schist.sync import init_standalone

        target = tmp_path / "my-vault"
        init_standalone(_args(path=str(target), name="my-vault", identity="local"))

        assert (target / "vault.yaml").is_file()
        assert (target / ".git").is_dir()
        assert (target / "notes" / ".gitkeep").is_file()
        assert (target / "concepts" / ".gitkeep").is_file()
        assert (target / "papers" / ".gitkeep").is_file()
        assert (target / ".gitignore").read_text() == ".schist/\n"

    def test_defaults_path_to_cwd(self, tmp_path, monkeypatch):
        from schist.sync import init_standalone

        cwd = tmp_path / "workdir"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        init_standalone(_args())

        assert (cwd / "vault.yaml").is_file()
        assert (cwd / ".git").is_dir()

    def test_defaults_name_from_dir(self, tmp_path):
        import yaml
        from schist.sync import init_standalone

        target = tmp_path / "alpha-vault"
        init_standalone(_args(path=str(target)))

        data = yaml.safe_load((target / "vault.yaml").read_text())
        assert data["name"] == "alpha-vault"

    def test_defaults_identity_to_local(self, tmp_path):
        import yaml
        from schist.sync import init_standalone

        target = tmp_path / "v"
        init_standalone(_args(path=str(target)))

        data = yaml.safe_load((target / "vault.yaml").read_text())
        assert data["participants"][0]["name"] == "local"
        assert "local" in data["access"]

    def test_rejects_invalid_identity_format(self, tmp_path, capsys):
        from schist.sync import init_standalone

        target = tmp_path / "v"
        with pytest.raises(SystemExit):
            init_standalone(_args(path=str(target), identity="Not_Valid"))
        err = capsys.readouterr().err
        assert "--identity" in err
        assert "^[a-z][a-z0-9-]*$" in err
        assert not target.exists()

    def test_rejects_nonempty_target(self, tmp_path, capsys):
        from schist.sync import init_standalone

        target = tmp_path / "existing"
        target.mkdir()
        (target / "file").write_text("already here")
        with pytest.raises(SystemExit):
            init_standalone(_args(path=str(target)))
        assert "already exists and is not empty" in capsys.readouterr().err

    def test_allows_empty_target_dir(self, tmp_path):
        from schist.sync import init_standalone

        target = tmp_path / "empty-target"
        target.mkdir()
        init_standalone(_args(path=str(target)))
        assert (target / "vault.yaml").is_file()

    def test_creates_intermediate_dirs(self, tmp_path):
        from schist.sync import init_standalone

        target = tmp_path / "a" / "b" / "c" / "vault"
        init_standalone(_args(path=str(target)))
        assert (target / "vault.yaml").is_file()

    def test_seed_vault_yaml_parses_and_validates(self, tmp_path):
        from schist.acl import parse_vault_yaml
        from schist.sync import init_standalone

        target = tmp_path / "v"
        init_standalone(_args(path=str(target), name="v", identity="yibei"))

        acl = parse_vault_yaml(target / "vault.yaml")
        assert acl.name == "v"
        assert acl.vault_version == 1
        assert {p.name for p in acl.participants} == {"yibei"}
        assert acl.can_write("yibei", "notes")
        assert acl.can_write("yibei", "concepts/x")
        assert acl.can_read("yibei", "papers")

    def test_directory_scaffold_committed_with_gitkeeps(self, tmp_path):
        from schist.sync import init_standalone

        target = tmp_path / "v"
        init_standalone(_args(path=str(target)))

        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "main"],
            cwd=target, capture_output=True, text=True, check=True,
        )
        tracked = set(result.stdout.splitlines())
        assert ".gitignore" in tracked
        assert "vault.yaml" in tracked
        assert "notes/.gitkeep" in tracked
        assert "concepts/.gitkeep" in tracked
        assert "papers/.gitkeep" in tracked

    def test_hooks_installed_and_executable(self, tmp_path):
        from schist.sync import init_standalone

        target = tmp_path / "v"
        init_standalone(_args(path=str(target)))

        post = target / ".git" / "hooks" / "post-commit"
        pre = target / ".git" / "hooks" / "pre-commit"
        assert post.is_file() and os.access(post, os.X_OK)
        assert pre.is_file() and os.access(pre, os.X_OK)
        assert post.read_text().startswith("#!/bin/sh")
        assert "schist post-commit hook" in post.read_text()
        assert "schist pre-commit hook" in pre.read_text()

    def test_hooks_not_committed(self, tmp_path):
        """Hooks live in .git/hooks/ (untracked by git by design)."""
        from schist.sync import init_standalone

        target = tmp_path / "v"
        init_standalone(_args(path=str(target)))

        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "main"],
            cwd=target, capture_output=True, text=True, check=True,
        )
        tracked = result.stdout.splitlines()
        assert not any("hooks/" in f for f in tracked)

    def test_vault_yaml_roundtrip(self, tmp_path):
        import yaml
        from schist.sync import init_standalone

        target = tmp_path / "v"
        init_standalone(_args(path=str(target), name="roundtrip", identity="local"))

        data = yaml.safe_load((target / "vault.yaml").read_text())
        assert data["vault_version"] == 1
        assert data["name"] == "roundtrip"
        assert data["scope_convention"] == "subdirectory"
        assert data["access"]["local"] == {"read": ["*"], "write": ["*"]}

    def test_initial_commit_on_main_branch(self, tmp_path):
        from schist.sync import init_standalone

        target = tmp_path / "v"
        init_standalone(_args(path=str(target), name="v"))

        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=target, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert branch == "main"

        msg = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=target, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert msg == "init: scaffold standalone vault v"

    def test_custom_identity_produces_correct_acl(self, tmp_path):
        from schist.acl import parse_vault_yaml
        from schist.sync import init_standalone

        target = tmp_path / "v"
        init_standalone(_args(path=str(target), identity="yibei"))
        acl = parse_vault_yaml(target / "vault.yaml")
        assert acl.can_write("yibei", "any/scope/here")
        assert not acl.can_write("someone-else", "any")

    def test_rollback_on_git_init_failure(self, tmp_path, monkeypatch, capsys):
        """If git init fails, staging dir cleaned up and target untouched."""
        from schist import sync as sync_mod

        target = tmp_path / "v"
        real_run = sync_mod.subprocess.run

        def fake_run(cmd, *a, **kw):
            if "init" in cmd and "--initial-branch=main" in cmd:
                return type("R", (), {
                    "returncode": 1, "stdout": "", "stderr": "simulated init failure",
                })()
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(sync_mod.subprocess, "run", fake_run)

        with pytest.raises(SystemExit):
            sync_mod.init_standalone(_args(path=str(target)))

        assert not target.exists()
        leftover = list(tmp_path.glob(".v.init-*"))
        assert not leftover, f"staging dir not cleaned: {leftover}"
        assert "simulated init failure" in capsys.readouterr().err

    def test_rollback_on_commit_failure(self, tmp_path, monkeypatch):
        """If the seed commit fails, staging cleaned up and target untouched."""
        from schist import sync as sync_mod

        target = tmp_path / "v"
        real_run = sync_mod.subprocess.run

        def fake_run(cmd, *a, **kw):
            if "commit" in cmd:
                return type("R", (), {
                    "returncode": 1, "stdout": "", "stderr": "simulated commit failure",
                })()
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(sync_mod.subprocess, "run", fake_run)

        with pytest.raises(SystemExit):
            sync_mod.init_standalone(_args(path=str(target)))

        assert not target.exists()
        assert not list(tmp_path.glob(".v.init-*"))

    def test_rejects_unwritable_parent_cleanly(self, tmp_path, monkeypatch, capsys):
        """PermissionError from staging.mkdir() surfaces as a clean error line,
        not a traceback. Regression test for the OSError-escapes-handler bug
        where `except _InitError` let filesystem errors propagate untouched.
        """
        from schist import sync as sync_mod

        target = tmp_path / "unwritable-parent" / "v"
        real_mkdir = Path.mkdir

        def fake_mkdir(self, *args, **kwargs):
            # Fire only for the staging dir; let target.parent.mkdir succeed.
            if ".v.init-" in self.name:
                raise PermissionError(13, "Permission denied", str(self))
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)

        with pytest.raises(SystemExit):
            sync_mod.init_standalone(_args(path=str(target)))

        err = capsys.readouterr().err
        assert err.startswith("Error: "), f"expected clean error line, got: {err!r}"
        assert "Permission denied" in err
        assert not target.exists()

    def test_renames_over_empty_target(self, tmp_path):
        """If the target exists but is empty, init still succeeds (atomic rename)."""
        from schist.sync import init_standalone

        target = tmp_path / "empty"
        target.mkdir()
        init_standalone(_args(path=str(target)))
        assert (target / "vault.yaml").is_file()

    def test_post_commit_constant_matches_disk(self):
        """Drift guard — in-memory POST_COMMIT_HOOK must equal the on-disk copy."""
        from schist.sync import POST_COMMIT_HOOK

        disk = (REPO_ROOT / "hooks" / "post-commit").read_text()
        assert POST_COMMIT_HOOK == disk, (
            "POST_COMMIT_HOOK in sync.py has drifted from hooks/post-commit. "
            "Update the constant so standalone-init-installed hooks match the "
            "repo reference."
        )

    def test_pre_commit_constant_matches_disk(self):
        """Drift guard — in-memory PRE_COMMIT_HOOK must equal the on-disk copy."""
        from schist.sync import PRE_COMMIT_HOOK

        disk = (REPO_ROOT / "hooks" / "pre-commit").read_text()
        assert PRE_COMMIT_HOOK == disk, (
            "PRE_COMMIT_HOOK in sync.py has drifted from hooks/pre-commit. "
            "Update the constant so standalone-init-installed hooks match the "
            "repo reference."
        )


class TestDispatchInit:
    """Conflict matrix for `schist init` across all three modes."""

    def test_hub_path_and_spoke_rejected(self, tmp_path, capsys):
        from schist.sync import _dispatch_init

        args = _args(hub_path=str(tmp_path / "hub.git"), spoke=True)
        with pytest.raises(SystemExit):
            _dispatch_init(args)
        assert "mutually exclusive" in capsys.readouterr().err

    def test_hub_path_and_hub_url_rejected(self, tmp_path, capsys):
        from schist.sync import _dispatch_init

        args = _args(hub_path=str(tmp_path / "hub.git"), hub="git@host:r.git")
        with pytest.raises(SystemExit):
            _dispatch_init(args)
        err = capsys.readouterr().err
        assert "--hub-path and --hub" in err

    def test_hub_url_without_spoke_rejected(self, capsys):
        """Pre-existing bug fix: `--hub URL` without `--spoke` would silently
        fall through and drop the URL. Now rejected up front."""
        from schist.sync import _dispatch_init

        args = _args(hub="git@host:r.git")
        with pytest.raises(SystemExit):
            _dispatch_init(args)
        assert "--hub requires --spoke" in capsys.readouterr().err

    def test_positional_path_with_hub_path_rejected(self, tmp_path, capsys):
        from schist.sync import _dispatch_init

        args = _args(path=str(tmp_path / "v"), hub_path=str(tmp_path / "hub.git"))
        with pytest.raises(SystemExit):
            _dispatch_init(args)
        assert "positional <path>" in capsys.readouterr().err

    def test_positional_path_with_spoke_rejected(self, tmp_path, capsys):
        from schist.sync import _dispatch_init

        args = _args(path=str(tmp_path / "v"), spoke=True, hub="git@host:r.git")
        with pytest.raises(SystemExit):
            _dispatch_init(args)
        assert "positional <path>" in capsys.readouterr().err

    def test_standalone_routes_to_init_standalone(self, tmp_path):
        from schist.sync import _dispatch_init

        target = tmp_path / "v"
        _dispatch_init(_args(path=str(target)))
        assert (target / "vault.yaml").is_file()

    def test_hub_mode_routes_to_init_hub(self, monkeypatch, tmp_path):
        """--hub-path routes to init_hub with the path forwarded."""
        from schist import sync as sync_mod

        called: dict = {}

        def fake_init_hub(args, hub_path):
            called["args"] = args
            called["hub_path"] = hub_path

        monkeypatch.setattr(sync_mod, "init_hub", fake_init_hub)
        sync_mod._dispatch_init(
            _args(hub_path=str(tmp_path / "hub.git"), name="v", participant=["a"])
        )
        assert called["hub_path"] == str(tmp_path / "hub.git")

    def test_spoke_mode_routes_to_init_spoke(self, monkeypatch):
        """--spoke routes to init_spoke with vault_path derived from --hub URL.

        Uses an HTTP-style URL so `os.path.basename(...).removesuffix('.git')`
        produces a clean 'foo'. SSH-style `git@host:foo.git` derivation is a
        pre-existing quirk of the inherited logic (colons aren't path
        separators) — out of scope for this PR.
        """
        from schist import sync as sync_mod

        called: dict = {}

        def fake_init_spoke(args, vault_path, db_path):
            called["args"] = args
            called["vault_path"] = vault_path
            called["db_path"] = db_path

        monkeypatch.setattr(sync_mod, "init_spoke", fake_init_spoke)
        sync_mod._dispatch_init(
            _args(spoke=True, hub="https://example.com/foo.git", scope="notes", identity="a")
        )
        assert called["vault_path"] == "foo"
        assert called["db_path"] == os.path.join("foo", ".schist", "schist.db")


class TestPrintMcpConfig:
    def test_prints_valid_json(self, tmp_path, capsys):
        from schist.sync import _dispatch_init

        fake_mcp = tmp_path / "fake-index.js"
        fake_mcp.write_text("// fake")
        _dispatch_init(_args(
            print_mcp_config=True,
            mcp_format="claude",
            vault=str(tmp_path),
            identity="test-agent",
            mcp_server_path=str(fake_mcp),
        ))
        captured = capsys.readouterr()
        assert "# Paste into" in captured.out
        # Find the JSON part (after the comment line)
        json_start = captured.out.index("{")
        data = json.loads(captured.out[json_start:])
        assert "mcpServers" in data
        assert "schist" in data["mcpServers"]
        assert data["mcpServers"]["schist"]["env"]["SCHIST_AGENT_ID"] == "test-agent"

    def test_cursor_format(self, tmp_path, capsys):
        from schist.sync import _dispatch_init

        fake_mcp = tmp_path / "fake-index.js"
        fake_mcp.write_text("// fake")
        _dispatch_init(_args(
            print_mcp_config=True,
            mcp_format="cursor",
            vault=str(tmp_path),
            identity="mac",
            mcp_server_path=str(fake_mcp),
        ))
        captured = capsys.readouterr()
        assert ".cursor/mcp.json" in captured.out

    def test_no_files_created(self, tmp_path, capsys):
        from schist.sync import _dispatch_init

        fake_mcp = tmp_path / "fake-index.js"
        fake_mcp.write_text("// fake")
        before = set(tmp_path.rglob("*"))
        _dispatch_init(_args(
            print_mcp_config=True,
            vault=str(tmp_path),
            identity="test",
            mcp_server_path=str(fake_mcp),
        ))
        after = set(tmp_path.rglob("*"))
        assert before == after

    def test_requires_vault(self, capsys):
        from schist.sync import _dispatch_init

        with pytest.raises(SystemExit):
            _dispatch_init(_args(print_mcp_config=True, vault=None))

    def test_explicit_mcp_path(self, tmp_path, capsys):
        from schist.sync import _dispatch_init

        fake_mcp = tmp_path / "fake-index.js"
        fake_mcp.write_text("// fake")
        _dispatch_init(_args(
            print_mcp_config=True,
            vault=str(tmp_path),
            identity="test",
            mcp_server_path=str(fake_mcp),
        ))
        captured = capsys.readouterr()
        assert str(fake_mcp) in captured.out
