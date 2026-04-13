"""Tests for `schist init --hub` (hub initialization)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def _has_git() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not available")


class TestInitHub:
    def test_rejects_missing_name(self, tmp_path, capsys):
        from schist.sync import init_hub

        args = SimpleNamespace(name=None, participant=["alpha"])
        with pytest.raises(SystemExit):
            init_hub(args, str(tmp_path / "hub.git"))
        assert "--name is required" in capsys.readouterr().err

    def test_rejects_missing_participants(self, tmp_path, capsys):
        from schist.sync import init_hub

        args = SimpleNamespace(name="vault", participant=None)
        with pytest.raises(SystemExit):
            init_hub(args, str(tmp_path / "hub.git"))
        assert "--participant is required" in capsys.readouterr().err

    def test_rejects_nonempty_hub_path(self, tmp_path, capsys):
        from schist.sync import init_hub

        hub = tmp_path / "hub.git"
        hub.mkdir()
        (hub / "file").write_text("existing")
        args = SimpleNamespace(name="vault", participant=["alpha"])
        with pytest.raises(SystemExit):
            init_hub(args, str(hub))
        assert "already exists and is not empty" in capsys.readouterr().err

    def test_creates_bare_repo_with_hook_and_seed(self, tmp_path):
        from schist.sync import init_hub

        hub = tmp_path / "hub.git"
        args = SimpleNamespace(name="research-graph", participant=["alpha", "beta"])
        init_hub(args, str(hub))

        # Bare repo structure
        assert (hub / "HEAD").is_file()
        assert (hub / "objects").is_dir()
        assert (hub / "refs" / "heads").is_dir()
        assert not (hub / ".git").exists()  # bare, no .git/

        # Pre-receive hook installed and executable
        hook = hub / "hooks" / "pre-receive"
        assert hook.is_file()
        assert os.access(hook, os.X_OK)
        assert "schist.pre_receive" in hook.read_text()

        # main branch has the seed commit with vault.yaml
        result = subprocess.run(
            ["git", "show", "main:vault.yaml"],
            cwd=hub, capture_output=True, text=True, check=True,
        )
        content = result.stdout
        assert "vault_version: 1" in content
        assert "name: research-graph" in content
        assert "alpha" in content
        assert "beta" in content
        assert "research/alpha" in content
        assert "research/beta" in content

    def test_seeded_yaml_passes_acl_validation(self, tmp_path):
        """The generated vault.yaml must parse as a valid v1 ACL."""
        from schist.acl import parse_vault_yaml
        from schist.sync import init_hub

        hub = tmp_path / "hub.git"
        args = SimpleNamespace(name="v", participant=["a", "b", "c"])
        init_hub(args, str(hub))

        # Clone to temp to inspect the committed vault.yaml on disk
        work = tmp_path / "work"
        subprocess.run(
            ["git", "clone", str(hub), str(work)],
            capture_output=True, text=True, check=True,
        )
        acl = parse_vault_yaml(work / "vault.yaml")
        assert acl.name == "v"
        assert {p.name for p in acl.participants} == {"a", "b", "c"}
        # Each participant can write only their own scope
        assert acl.can_write("a", "research/a")
        assert not acl.can_write("a", "research/b")
        # All can read everything
        assert acl.can_read("a", "research/b")

    def test_hub_push_then_rejects_out_of_scope(self, tmp_path):
        """After init_hub, a spoke push outside its scope is rejected."""
        from schist.sync import init_hub

        hub = tmp_path / "hub.git"
        init_hub(
            SimpleNamespace(name="v", participant=["alpha", "beta"]),
            str(hub),
        )

        # Clone as alpha, try to write beta's scope
        work = tmp_path / "work-alpha"
        subprocess.run(["git", "clone", str(hub), str(work)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "a@test"], cwd=work, check=True)
        subprocess.run(["git", "config", "user.name", "a"], cwd=work, check=True)

        (work / "research" / "beta").mkdir(parents=True)
        (work / "research" / "beta" / "note.md").write_text("# hi\n")
        subprocess.run(["git", "add", "research/beta/note.md"], cwd=work, check=True)
        subprocess.run(
            ["git", "commit", "-m", "invalid cross-scope write"],
            cwd=work, check=True, capture_output=True,
        )
        env = os.environ.copy()
        env["SCHIST_IDENTITY"] = "alpha"
        result = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=work, env=env, capture_output=True, text=True,
        )
        assert result.returncode != 0
        assert "REJECTED" in result.stderr or "rejected" in result.stderr
