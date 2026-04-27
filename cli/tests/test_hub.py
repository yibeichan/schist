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

    def test_failed_init_leaves_no_hub_path(self, tmp_path, capsys, monkeypatch):
        """If init_hub fails partway, hub_path must not exist (no half-init).

        Simulates a failure in the seed-push step by forcing a bad hub name.
        After the failure, the hub_path must not exist, so a retry with
        corrected args succeeds without manual cleanup.
        """
        from schist.sync import init_hub

        hub = tmp_path / "hub.git"

        # Force failure inside _build_hub_in_staging by monkeypatching
        # subprocess.run to fail on the seed push.
        import schist.sync as sync_mod
        real_run = sync_mod.subprocess.run
        call_count = {"n": 0}

        def fake_run(cmd, *a, **kw):
            call_count["n"] += 1
            # Let the git init --bare on staging succeed
            if "init" in cmd and "--bare" in cmd:
                return real_run(cmd, *a, **kw)
            # Simulate total failure for everything else
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "simulated failure"})()

        monkeypatch.setattr(sync_mod.subprocess, "run", fake_run)

        args = SimpleNamespace(name="vault", participant=["alpha"])
        with pytest.raises(SystemExit):
            init_hub(args, str(hub))

        # Crucial: hub_path must not exist after failure
        assert not hub.exists(), "failed init_hub left a half-initialized hub_path"

        # And no staging directory littered alongside
        leftover = list(tmp_path.glob(".hub.git.init-*"))
        assert not leftover, f"staging dir not cleaned up: {leftover}"

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

    # --- --scope-prefix tests (Issue #34) ---

    def test_scope_prefix_default(self, tmp_path):
        """Without --scope-prefix, vault.yaml uses research/<name> (backwards compat)."""
        from schist.sync import init_hub

        hub = tmp_path / "hub.git"
        args = SimpleNamespace(name="v", participant=["alpha"], scope_prefix="research")
        init_hub(args, str(hub))

        result = subprocess.run(
            ["git", "show", "main:vault.yaml"],
            cwd=hub, capture_output=True, text=True, check=True,
        )
        content = result.stdout
        assert "research/alpha" in content
        assert "vault_version: 1" in content

    def test_scope_prefix_override(self, tmp_path):
        """With --scope-prefix vault, vault.yaml uses vault/<name> instead of research/<name>."""
        from schist.sync import init_hub

        hub = tmp_path / "hub.git"
        args = SimpleNamespace(name="v", participant=["alpha", "beta"], scope_prefix="vault")
        init_hub(args, str(hub))

        result = subprocess.run(
            ["git", "show", "main:vault.yaml"],
            cwd=hub, capture_output=True, text=True, check=True,
        )
        content = result.stdout
        assert "vault/alpha" in content
        assert "vault/beta" in content
        assert "research/" not in content

    def test_scope_prefix_invalid(self, tmp_path, capsys):
        """Invalid --scope-prefix (spaces, dots, uppercase) is rejected."""
        from schist.sync import init_hub

        for bad in ["BAD NAME", "../evil", "has.dots", "UPPER"]:
            hub = tmp_path / f"hub-{bad.replace('/', '_')}"
            args = SimpleNamespace(name="v", participant=["alpha"], scope_prefix=bad)
            with pytest.raises(SystemExit):
                init_hub(args, str(hub))
            assert not hub.exists(), f"hub created despite invalid prefix '{bad}'"
            capsys.readouterr()  # clear output

    def test_scope_prefix_seeded_yaml_passes_acl(self, tmp_path):
        """Vault.yaml with custom scope_prefix passes ACL validation."""
        from schist.acl import parse_vault_yaml
        from schist.sync import init_hub

        hub = tmp_path / "hub.git"
        args = SimpleNamespace(name="v", participant=["a", "b"], scope_prefix="my-vault")
        init_hub(args, str(hub))

        work = tmp_path / "work"
        subprocess.run(
            ["git", "clone", str(hub), str(work)],
            capture_output=True, text=True, check=True,
        )
        acl = parse_vault_yaml(work / "vault.yaml")
        assert acl.name == "v"
        assert acl.can_write("a", "my-vault/a")
        assert not acl.can_write("a", "my-vault/b")
        assert acl.can_read("a", "my-vault/b")
