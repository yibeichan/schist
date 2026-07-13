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
        # Flat seed: content-axis dirs, no per-participant subdirectories
        assert "scope_convention: flat" in content
        assert "default_scope: global" in content
        assert "research/alpha" not in content
        assert "research/beta" not in content

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
        # Flat seed: all participants write the content-axis dirs
        assert acl.can_write("a", "research")
        assert acl.can_write("b", "research")
        assert acl.can_write("a", "concepts")
        # All can read everything
        assert acl.can_read("a", "research")
        assert acl.scope_convention == "flat"

    def test_seed_commit_gitignores_schist_dir(self, tmp_path):
        """#309: spokes clone the hub as their working tree, so the seed
        commit must ship a .gitignore covering the .schist/ runtime dir
        (SQLite index + WAL siblings) — cone-mode sparse checkout always
        materializes root files, so every spoke inherits it."""
        from schist.sync import init_hub

        hub = tmp_path / "hub.git"
        args = SimpleNamespace(name="v", participant=["alpha"])
        init_hub(args, str(hub))

        result = subprocess.run(
            ["git", "show", "main:.gitignore"],
            cwd=hub, capture_output=True, text=True, check=True,
        )
        assert ".schist/" in result.stdout.splitlines()

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

    def test_final_install_race_cleans_staging(self, tmp_path, monkeypatch, capsys):
        """If hub_path becomes non-empty after the pre-check, init_hub should
        clean staging and report a user-facing install error."""
        from schist import sync as sync_mod

        hub = tmp_path / "hub.git"

        def fake_build(staging, hub_path, vault_data, participants, name):
            staging.mkdir(parents=True)
            (staging / "HEAD").write_text("ref: refs/heads/main\n")
            hub.mkdir()
            (hub / "intruder").write_text("raced\n")

        monkeypatch.setattr(sync_mod, "_build_hub_in_staging", fake_build)

        args = SimpleNamespace(name="vault", participant=["alpha"])
        with pytest.raises(SystemExit):
            sync_mod.init_hub(args, str(hub))

        err = capsys.readouterr().err
        assert "failed to install vault" in err
        assert not list(tmp_path.glob(".hub.git.init-*"))
        assert (hub / "intruder").read_text() == "raced\n"

    def test_hub_push_then_rejects_out_of_scope(self, tmp_path):
        """After init_hub, a spoke push to a dir outside the content-axis write list is rejected.

        Under flat seed, all participants share the content-axis dirs (research/, concepts/, etc.).
        A push to an unlisted dir (e.g. shared/) must still be rejected.
        """
        from schist.sync import init_hub

        hub = tmp_path / "hub.git"
        init_hub(
            SimpleNamespace(name="v", participant=["alpha", "beta"]),
            str(hub),
        )

        # Clone as alpha, try to write to 'shared/' which is not in the content-axis write list
        work = tmp_path / "work-alpha"
        subprocess.run(["git", "clone", str(hub), str(work)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "a@test"], cwd=work, check=True)
        subprocess.run(["git", "config", "user.name", "a"], cwd=work, check=True)

        (work / "shared").mkdir(parents=True)
        (work / "shared" / "note.md").write_text("# hi\n")
        subprocess.run(["git", "add", "shared/note.md"], cwd=work, check=True)
        subprocess.run(
            ["git", "commit", "-m", "invalid out-of-scope write"],
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

    # --- --scope-prefix tests (Issue #34, updated for flat default) ---
    # scope_prefix is now a no-op: the CLI flag is accepted for backward-compat
    # but ignored. All new hubs seed with scope_convention: flat.

    def test_scope_prefix_ignored_flat_seed(self, tmp_path):
        """--scope-prefix is accepted but ignored; vault still uses flat convention."""
        from schist.sync import init_hub

        hub = tmp_path / "hub.git"
        args = SimpleNamespace(name="v", participant=["alpha"], scope_prefix="research")
        init_hub(args, str(hub))

        result = subprocess.run(
            ["git", "show", "main:vault.yaml"],
            cwd=hub, capture_output=True, text=True, check=True,
        )
        content = result.stdout
        assert "scope_convention: flat" in content
        assert "research/alpha" not in content
        assert "vault_version: 1" in content

    def test_scope_prefix_override_still_flat(self, tmp_path):
        """--scope-prefix override is accepted but ignored; seed is still flat."""
        from schist.sync import init_hub

        hub = tmp_path / "hub.git"
        args = SimpleNamespace(name="v", participant=["alpha", "beta"], scope_prefix="vault")
        init_hub(args, str(hub))

        result = subprocess.run(
            ["git", "show", "main:vault.yaml"],
            cwd=hub, capture_output=True, text=True, check=True,
        )
        content = result.stdout
        assert "scope_convention: flat" in content
        assert "vault/alpha" not in content
        assert "vault/beta" not in content

    def test_scope_prefix_seeded_yaml_passes_acl(self, tmp_path):
        """Vault.yaml seeded by init_hub (with any scope_prefix) passes ACL validation."""
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
        assert acl.scope_convention == "flat"
        # All participants can write content-axis dirs
        assert acl.can_write("a", "research")
        assert acl.can_write("b", "research")
        # Nobody can write 'my-vault' (not in content-axis list)
        assert not acl.can_write("a", "my-vault/a")

    def test_scope_prefix_custom_warns(self, tmp_path, capsys):
        """A non-default --scope-prefix is deprecated; user must be warned it's
        ignored. The warning is surfaced once at dispatch so every init mode
        (hub/spoke/standalone) emits it consistently."""
        from schist.sync import _dispatch_init

        hub = tmp_path / "hub.git"
        args = SimpleNamespace(
            print_mcp_config=False, hub_path=str(hub), spoke=False, hub=None,
            path=None, name="v", participant=["alpha"], scope_prefix="custom-thing",
        )
        _dispatch_init(args)

        captured = capsys.readouterr()
        assert "deprecated" in captured.err.lower()
        # The seed must still be flat despite the custom prefix
        result = subprocess.run(
            ["git", "show", "main:vault.yaml"],
            cwd=hub, capture_output=True, text=True, check=True,
        )
        assert "scope_convention: flat" in result.stdout
        assert "custom-thing" not in result.stdout

    def test_scope_prefix_default_no_warn(self, tmp_path, capsys):
        """Default scope_prefix must NOT emit the deprecation warning (no noise)."""
        from schist.sync import _dispatch_init

        hub = tmp_path / "hub.git"
        args = SimpleNamespace(
            print_mcp_config=False, hub_path=str(hub), spoke=False, hub=None,
            path=None, name="v", participant=["alpha"], scope_prefix="research",
        )
        _dispatch_init(args)

        captured = capsys.readouterr()
        assert "deprecated" not in captured.err.lower()

    def test_scope_prefix_warns_in_standalone_mode(self, tmp_path, capsys):
        """Regression for the prior hub-only placement: a non-default
        --scope-prefix must warn in non-hub init modes too. Standalone init is
        the cheapest non-hub path (no network), so it proves the generalization."""
        from schist.sync import _dispatch_init

        vault = tmp_path / "standalone"
        args = SimpleNamespace(
            print_mcp_config=False, hub_path=None, spoke=False, hub=None,
            path=str(vault), name="v", identity="local", scope_prefix="custom-thing",
        )
        _dispatch_init(args)

        captured = capsys.readouterr()
        assert "deprecated" in captured.err.lower()


class TestInitSubprocessTimeouts:
    """#371: every subprocess in the init paths must be bounded, and a
    timeout must surface as clean cleanup — an NFS stall otherwise hung
    `schist hub init` / `schist init` forever, and a raw TimeoutExpired
    escaping the _InitError/OSError except clause left the staging dir
    behind."""

    def test_hub_init_all_subprocess_calls_carry_timeouts(self, tmp_path, monkeypatch):
        import schist.sync as sync_mod

        real_run = sync_mod.subprocess.run
        calls: list[tuple[list, object]] = []

        def record(cmd, *a, **kw):
            calls.append((cmd, kw.get("timeout")))
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(sync_mod.subprocess, "run", record)
        args = SimpleNamespace(name="vault", participant=["alpha"])
        sync_mod.init_hub(args, str(tmp_path / "hub.git"))

        assert calls, "expected subprocess calls during hub init"
        for cmd, timeout in calls:
            assert timeout is not None and timeout > 0, f"{cmd} ran with no timeout"
        # The seed push runs the pre-receive hook (cold python + import
        # schist), so it gets the hook-running 120s tier like
        # git_ops.COMMIT_TIMEOUT — not the 60s lock-shaped tier.
        push_timeouts = [t for cmd, t in calls if cmd[:2] == ["git", "push"]]
        assert push_timeouts == [120]

    def test_standalone_init_all_subprocess_calls_carry_timeouts(self, tmp_path, monkeypatch):
        import schist.sync as sync_mod

        real_run = sync_mod.subprocess.run
        calls: list[tuple[list, object]] = []

        def record(cmd, *a, **kw):
            calls.append((cmd, kw.get("timeout")))
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(sync_mod.subprocess, "run", record)
        args = SimpleNamespace(name="vault", identity="local", path=str(tmp_path / "v"))
        sync_mod.init_standalone(args)

        assert calls, "expected subprocess calls during standalone init"
        for cmd, timeout in calls:
            assert timeout is not None and timeout > 0, f"{cmd} ran with no timeout"

    def test_hub_init_timeout_cleans_staging_and_reports(self, tmp_path, monkeypatch, capsys):
        """A stalled subprocess must land in init_hub's cleanup path — clean
        error, no hub_path, no staging litter — not a raw TimeoutExpired."""
        import schist.sync as sync_mod

        real_run = sync_mod.subprocess.run

        def stall_on_seed_push(cmd, *a, **kw):
            if cmd[:2] == ["git", "push"]:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout"))
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(sync_mod.subprocess, "run", stall_on_seed_push)
        hub = tmp_path / "hub.git"
        args = SimpleNamespace(name="vault", participant=["alpha"])
        with pytest.raises(SystemExit):
            sync_mod.init_hub(args, str(hub))

        assert "timed out" in capsys.readouterr().err
        assert not hub.exists()
        assert not list(tmp_path.glob(".hub.git.init-*"))

    def test_standalone_init_timeout_cleans_staging_and_reports(self, tmp_path, monkeypatch, capsys):
        import schist.sync as sync_mod

        real_run = sync_mod.subprocess.run

        def stall_on_commit(cmd, *a, **kw):
            if cmd[:2] == ["git", "commit"]:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout"))
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(sync_mod.subprocess, "run", stall_on_commit)
        target = tmp_path / "v"
        args = SimpleNamespace(name="vault", identity="local", path=str(target))
        with pytest.raises(SystemExit):
            sync_mod.init_standalone(args)

        assert "timed out" in capsys.readouterr().err
        assert not target.exists()
