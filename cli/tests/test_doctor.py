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
    _mcp_dist_dir_from_config,
    check_git,
    check_hooks_freshness,
    check_hooks_path,
    check_hooks_path,
    check_ingest_available,
    check_mcp_config,
    check_mcp_schema_alignment,
    check_mcp_vocab_alignment,
    check_node,
    check_post_commit_hook,
    check_pre_commit_hook,
    _effective_hooks_dir,
    check_hub_pre_receive_hook,
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


def _real_repo(path: Path) -> Path:
    """A REAL git repo. The hook checks ask git where it looks for hooks, and
    git refuses a directory that only *looks* like a repo — several fixtures
    here used to `mkdir .git/hooks` and were silently tolerated by the old
    hand-rolled path resolution."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _real_bare_hub(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return path


class TestCheckPostCommitHook:
    def test_no_path(self):
        r = check_post_commit_hook(None)
        assert r.status == "SKIP"

    def test_installed(self, tmp_path):
        hooks = _real_repo(tmp_path) / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "post-commit"
        hook.write_text("#!/bin/sh\n")
        # chmod added with #460: git only runs an executable hook, so a
        # non-executable one is not an "installed" hook in any useful sense.
        hook.chmod(0o755)
        r = check_post_commit_hook(str(tmp_path))
        assert r.status == "PASS"

    def test_installed_but_not_executable(self, tmp_path):
        """#460: git SKIPS a non-executable hook silently, so auto-ingest is
        dead while doctor reports PASS and the index drifts from the notes."""
        import os
        hooks = _real_repo(tmp_path) / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "post-commit"
        hook.write_text("#!/bin/sh\n")
        hook.chmod(0o644)
        if os.access(hook, os.X_OK):  # pragma: no cover - root ignores mode bits
            import pytest as _pytest
            _pytest.skip("running as root: mode bits don't gate os.access")
        r = check_post_commit_hook(str(tmp_path))
        assert r.status == "FAIL"
        assert "not executable" in r.message
        assert "chmod +x" in (r.fix or "")

    def test_missing(self, tmp_path):
        _real_repo(tmp_path)
        r = check_post_commit_hook(str(tmp_path))
        assert r.status == "FAIL"


class TestCheckPreCommitHook:
    """#536 — the pre-commit hook is the staged-secret scanner, and doctor had
    no check for it at all. `check_hooks_freshness` reads its version marker,
    which needs only READ permission, so a hook git would never run reported
    PASS."""

    def test_no_path(self):
        r = check_pre_commit_hook(None)
        assert r.status == "SKIP"

    def test_installed(self, tmp_path):
        hooks = _real_repo(tmp_path) / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\n")
        hook.chmod(0o755)
        assert check_pre_commit_hook(str(tmp_path)).status == "PASS"

    def test_installed_but_not_executable(self, tmp_path):
        """The severe half of #460: git skips it silently, so `git commit`
        stops scanning for secrets and says nothing."""
        import os
        hooks = _real_repo(tmp_path) / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\n")
        hook.chmod(0o644)
        if os.access(hook, os.X_OK):  # pragma: no cover - root ignores mode bits
            import pytest as _pytest
            _pytest.skip("running as root: mode bits don't gate os.access")
        r = check_pre_commit_hook(str(tmp_path))
        assert r.status == "FAIL"
        assert "not executable" in r.message
        assert "NOT being scanned" in r.message
        assert "chmod +x" in (r.fix or "")

    def test_missing(self, tmp_path):
        _real_repo(tmp_path)
        r = check_pre_commit_hook(str(tmp_path))
        assert r.status == "FAIL"
        # Not vacuous: it must fail for ABSENCE, not because the fixture is
        # not a repo (which is its own, differently-worded failure).
        assert "not installed" in r.message


class TestHookChecksRespectHooksPath:
    """#532 — every hook check inspected .git/hooks unconditionally. With
    core.hooksPath set, git runs hooks from somewhere else, so those checks
    reported on files git will never execute: a PASS that is a false assurance
    about the secret scanner, sitting next to a WARN that reads as 'unusual but
    fine'."""

    def _repo(self, tmp_path, hooks_path_value):
        import subprocess
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config",
                        "core.hooksPath", hooks_path_value], check=True)
        return tmp_path

    def test_relative_hooks_path_is_where_the_hook_is_found(self, tmp_path):
        repo = self._repo(tmp_path, "team-hooks")
        # The DEFAULT location has a good hook; git will not run it.
        default = repo / ".git" / "hooks"
        default.mkdir(parents=True, exist_ok=True)
        (default / "pre-commit").write_text("#!/bin/sh\n")
        (default / "pre-commit").chmod(0o755)
        r = check_pre_commit_hook(str(repo))
        assert r.status == "FAIL", "must not pass on a hook git never runs"
        assert "team-hooks" in r.message
        # `hooks reinstall` writes to .git/hooks, so recommending it here would
        # send the user to install a hook where git is not looking.
        assert "symlink" in (r.fix or "")
        assert "hooks reinstall" in (r.fix or "")

        # Put it where git actually looks.
        real = repo / "team-hooks"
        real.mkdir()
        (real / "pre-commit").write_text("#!/bin/sh\n")
        (real / "pre-commit").chmod(0o755)
        r = check_pre_commit_hook(str(repo))
        assert r.status == "PASS"
        assert "core.hooksPath" in r.message

    def test_empty_hooks_path_is_not_the_same_as_unset(self, tmp_path):
        """Verified against real git: `core.hooksPath = ""` runs NO hook from
        .git/hooks. Treating set-but-empty as unset made doctor report
        `PASS | installed` on the default pre-commit while git ran nothing —
        a false all-clear on a disabled secret scanner, which is the exact
        defect class this check exists to catch."""
        repo = self._repo(tmp_path, "")
        default = repo / ".git" / "hooks"
        default.mkdir(parents=True, exist_ok=True)
        for name in ("pre-commit", "post-commit"):
            (default / name).write_text("#!/bin/sh\n")
            (default / name).chmod(0o755)

        for check in (check_pre_commit_hook, check_post_commit_hook):
            r = check(str(repo))
            assert r.status == "FAIL", f"{check.__name__} must not pass on a disabled hook"
            assert "empty value" in r.message
            assert "--unset core.hooksPath" in (r.fix or "")
        assert check_hooks_freshness(str(repo)).status == "FAIL"

    def test_hooks_path_check_agrees_with_the_hook_checks(self, tmp_path):
        """One doctor run must not contradict itself. check_hooks_path did its
        own `--get` + truthiness test, so with an empty core.hooksPath three
        checks FAILed "no hooks run" while it reported "PASS | uses default
        .git/hooks/" — the same empty-vs-unset defect, surviving in the
        sibling check three functions away from the ones that fixed it."""
        repo = self._repo(tmp_path, "")
        assert check_hooks_path(str(repo)).status == "FAIL"
        assert check_pre_commit_hook(str(repo)).status == "FAIL"

    def test_empty_hookspath_message_names_no_directory(self, tmp_path):
        """#550: the message hardcoded ".git/hooks", but this string is shared
        with the bare-repo hub check, whose hooks live at <repo>/hooks — so it
        stated a path that does not exist there. It must describe the
        condition, not guess a location."""
        import subprocess as sp
        hub = _real_bare_hub(tmp_path / "hub.git")
        sp.run(["git", "--git-dir", str(hub), "config", "core.hooksPath", ""], check=True)
        r = check_hub_pre_receive_hook(str(hub))
        assert r.status == "FAIL"
        assert ".git/hooks" not in r.message
        assert "no hooks directory" in r.message
        assert "pre-receive" in r.message

    def test_stale_fix_names_the_hook_that_is_actually_stale(self, tmp_path):
        """#548: the stale remedy was built for "pre-commit" unconditionally,
        so a vault whose POST-commit was stale got instructions pointing at
        the other hook — and under a redirect those instructions carry a
        symlink path, making the wrong name actively misleading."""
        from schist import sync as sync_mod
        repo = _real_repo(tmp_path)
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        # pre-commit current, post-commit stale.
        (hooks / "pre-commit").write_text(
            f"#!/bin/sh\n# schist-hook-version: {sync_mod.HOOK_VERSION}\n")
        (hooks / "post-commit").write_text("#!/bin/sh\n# schist-hook-version: 1\n")
        r = check_hooks_freshness(str(repo))
        assert r.status == "WARN"
        assert "post-commit" in r.message
        assert "post-commit" in (r.fix or ""), r.fix
        assert "pre-commit" not in (r.fix or ""), r.fix

    def test_a_directory_named_like_a_hook_is_not_a_hook(self, tmp_path):
        """os.access(X_OK) is true for a directory, so without an is_file()
        guard a directory named `pre-commit` reported PASS | installed."""
        repo = _real_repo(tmp_path)
        d = repo / ".git" / "hooks"
        d.mkdir(parents=True, exist_ok=True)
        (d / "pre-commit").mkdir()
        r = check_pre_commit_hook(str(repo))
        assert r.status == "FAIL"
        assert "not a file" in r.message

    def test_tilde_is_expanded_by_git_not_by_us(self, tmp_path):
        """`--type=path` makes git do the expansion, so we never have to match
        Path.expanduser() against git's behaviour by hand."""
        hooks = Path.home() / ".schist-doctor-tilde-test"
        repo = self._repo(tmp_path, "~/.schist-doctor-tilde-test")
        hooks_dir, configured, error = _effective_hooks_dir(str(repo))
        assert hooks_dir == hooks, hooks_dir
        assert "~" not in str(hooks_dir)
        assert configured == str(hooks)

    def test_last_value_wins_on_a_multivalued_key(self, tmp_path):
        """git honours the last value for a single-valued key, and so must we."""
        import subprocess as sp
        repo = self._repo(tmp_path, "first")
        sp.run(["git", "-C", str(repo), "config", "--add",
                "core.hooksPath", "second"], check=True)
        hooks_dir, _, _err = _effective_hooks_dir(str(repo))
        assert hooks_dir == repo / "second", hooks_dir

    def test_absolute_hooks_path(self, tmp_path):
        hooks = tmp_path / "elsewhere"
        hooks.mkdir()
        (hooks / "post-commit").write_text("#!/bin/sh\n")
        (hooks / "post-commit").chmod(0o755)
        repo = self._repo(tmp_path / "repo", str(hooks))
        r = check_post_commit_hook(str(repo))
        assert r.status == "PASS"
        assert str(hooks) in r.message

    def test_freshness_reads_the_redirected_dir(self, tmp_path):
        """Otherwise it reports the version of a hook that never runs."""
        from schist import sync as sync_mod
        repo = self._repo(tmp_path, "team-hooks")
        real = repo / "team-hooks"
        real.mkdir()
        for name in ("pre-commit", "post-commit"):
            (real / name).write_text(
                f"#!/bin/sh\n# schist-hook-version: {sync_mod.HOOK_VERSION}\n")
            (real / name).chmod(0o755)
        # A stale hook sits at the default path and must be ignored.
        default = repo / ".git" / "hooks"
        default.mkdir(parents=True, exist_ok=True)
        for name in ("pre-commit", "post-commit"):
            (default / name).write_text("#!/bin/sh\n")
        assert check_hooks_freshness(str(repo)).status == "PASS"


class TestCheckHubPreReceiveHook:
    """#538 — the hub's pre-receive hook IS the write ACL. Git ignores a hook
    it cannot execute and CONTINUES, so a dropped exec bit means every push is
    accepted with no identity, scope or rate-limit check. Everything
    #502/#511/#519/#524 built rests on this hook running, and nothing checked
    it."""

    def test_no_hub_path(self):
        assert check_hub_pre_receive_hook(None).status == "SKIP"

    def test_not_a_repo(self, tmp_path):
        assert check_hub_pre_receive_hook(str(tmp_path)).status == "SKIP"

    def test_executable_passes(self, tmp_path):
        hub = _real_bare_hub(tmp_path / "hub.git")
        h = hub / "hooks" / "pre-receive"
        h.write_text("#!/bin/sh\nexec python3 -m schist.pre_receive\n")
        h.chmod(0o755)
        assert check_hub_pre_receive_hook(str(hub)).status == "PASS"

    def test_executable_but_EMPTY_is_the_worst_case(self, tmp_path):
        """The exec bit is necessary, not sufficient. The threat list for this
        check — archive restore, scp, container COPY, manual redeploy — causes
        truncation at least as often as mode loss, and truncation is the worse
        half: garbage content fails CLOSED (the shell errors, git reports
        "pre-receive hook declined"), while an empty script exits 0 and the
        push is accepted. Verified on real git: a 0-byte mode-755 pre-receive
        moves the ref."""
        hub = _real_bare_hub(tmp_path / "hub.git")
        h = hub / "hooks" / "pre-receive"
        h.write_text("")
        h.chmod(0o755)
        r = check_hub_pre_receive_hook(str(hub))
        assert r.status == "FAIL"
        assert "EMPTY" in r.message

    def test_executable_but_not_schist_warns(self, tmp_path):
        """Someone else's pre-receive is not schist's ACL. Not a FAIL — a hub
        admin may legitimately chain hooks — but it must not read as PASS."""
        hub = _real_bare_hub(tmp_path / "hub.git")
        h = hub / "hooks" / "pre-receive"
        h.write_text("#!/bin/sh\n# some other team's hook\nexit 0\n")
        h.chmod(0o755)
        r = check_hub_pre_receive_hook(str(hub))
        assert r.status == "WARN"
        assert "pre_receive" in r.message

    def test_hub_honours_core_hookspath(self, tmp_path):
        """The hub resolves through the same `rev-parse --git-path hooks` call
        as the spoke, so a redirected hub is found rather than reported
        missing."""
        import subprocess as sp
        hub = _real_bare_hub(tmp_path / "hub.git")
        elsewhere = tmp_path / "hub-hooks"
        elsewhere.mkdir()
        sp.run(["git", "--git-dir", str(hub), "config",
                "core.hooksPath", str(elsewhere)], check=True)
        h = elsewhere / "pre-receive"
        h.write_text("#!/bin/sh\nexec python3 -m schist.pre_receive\n")
        h.chmod(0o755)
        r = check_hub_pre_receive_hook(str(hub))
        assert r.status == "PASS", r.message
        assert str(elsewhere) in r.message

    def test_redirected_hub_with_no_hook_there_fails(self, tmp_path):
        """#554: the PASS side of the hub's core.hooksPath handling was
        covered, the FAIL side was not — and the FAIL side is the dangerous
        one. A hub redirected at a directory with no pre-receive in it has no
        ACL, even though a perfectly good hook still sits at <repo>/hooks."""
        import subprocess as sp
        hub = _real_bare_hub(tmp_path / "hub.git")
        good = hub / "hooks" / "pre-receive"
        good.write_text("#!/bin/sh\nexec python3 -m schist.pre_receive\n")
        good.chmod(0o755)
        elsewhere = tmp_path / "hub-hooks"
        elsewhere.mkdir()
        sp.run(["git", "--git-dir", str(hub), "config",
                "core.hooksPath", str(elsewhere)], check=True)
        r = check_hub_pre_receive_hook(str(hub))
        assert r.status == "FAIL", r.message
        assert "NO ACL" in r.message
        assert str(elsewhere) in r.message
        # #553: the remedy must not name `hooks reinstall`, which writes only
        # pre/post-commit and hard-exits on a bare repo.
        assert "hooks reinstall" not in (r.fix or "")
        assert str(elsewhere) in (r.fix or "")

    def test_hub_remedy_never_recommends_the_spoke_command(self, tmp_path):
        """#553: `hooks reinstall` writes pre-commit and post-commit only, and
        hard-exits with "not a git repository" when <path>/.git is absent —
        true of every bare hub. So the old remedy named a command that errors
        out immediately and would not have helped if it ran. There is no
        hub-hook reinstall command at all; the hook is written once by
        `schist init --hub`."""
        hub = _real_bare_hub(tmp_path / "hub.git")
        for stale in (hub / "hooks").glob("pre-receive*"):
            stale.unlink()
        r = check_hub_pre_receive_hook(str(hub))
        assert r.status == "FAIL"
        assert "hooks reinstall" not in (r.fix or ""), r.fix
        assert "--vault" not in (r.fix or ""), r.fix
        assert "schist init --hub" in (r.fix or ""), r.fix

    def test_unreadable_repo_is_not_reported_as_healthy(self, tmp_path):
        """`git config --get` exits 1 for "unset" and 128 for "I cannot read
        this repo" — a non-repo, an unreadable config, safe.directory
        ownership. Collapsing those into "unset" made the check fall back to
        the default path, find a hook and report PASS. For a check whose job
        is to say whether the ACL is live, "I don't know" must not be PASS."""
        hub = _real_bare_hub(tmp_path / "hub.git")
        h = hub / "hooks" / "pre-receive"
        h.write_text("#!/bin/sh\nexec python3 -m schist.pre_receive\n")
        h.chmod(0o755)
        (hub / "config").chmod(0o000)
        try:
            r = check_hub_pre_receive_hook(str(hub))
        finally:
            (hub / "config").chmod(0o644)
        assert r.status == "FAIL"
        assert "cannot determine" in r.message

    def test_not_executable_is_a_total_bypass(self, tmp_path):
        import os
        hub = _real_bare_hub(tmp_path / "hub.git")
        h = hub / "hooks" / "pre-receive"
        h.write_text("#!/bin/sh\nexit 1\n")
        h.chmod(0o644)
        if os.access(h, os.X_OK):  # pragma: no cover - root ignores mode bits
            import pytest as _pytest
            _pytest.skip("running as root: mode bits don't gate os.access")
        r = check_hub_pre_receive_hook(str(hub))
        assert r.status == "FAIL"
        assert "not executable" in r.message
        assert "NO ACL" in r.message

    def test_missing_is_also_a_bypass(self, tmp_path):
        hub = _real_bare_hub(tmp_path / "hub.git")
        r = check_hub_pre_receive_hook(str(hub))
        assert r.status == "FAIL"
        assert "NO ACL" in r.message


class TestCheckHooksFreshness:
    """Issue #103 — detect spokes still running an older hook template so
    fixes to the secret regex actually reach existing installations.

    NOTE: this check's core.hooksPath redirect behaviour is covered in
    TestHookChecksRespectHooksPath — `test_freshness_reads_the_redirected_dir`
    (reads the directory git actually uses) and
    `test_stale_fix_names_the_hook_that_is_actually_stale` (the remedy under a
    redirect). They live there because they share that class's redirect
    fixture; noted here because reading only this class made the coverage look
    missing (#549).
    """

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
        _real_repo(tmp_path)
        self._install_hook(tmp_path, "pre-commit", PRE_COMMIT_HOOK)
        self._install_hook(tmp_path, "post-commit", POST_COMMIT_HOOK)
        r = check_hooks_freshness(str(tmp_path))
        assert r.status == "PASS"
        assert f"v{HOOK_VERSION}" in r.message

    def test_legacy_unversioned_hook_warns(self, tmp_path):
        """A spoke init'd before HOOK_VERSION was introduced has no marker —
        must surface as stale so the user knows to reinstall."""
        _real_repo(tmp_path)
        self._install_hook(tmp_path, "pre-commit",
                           "#!/bin/sh\n# legacy hook with no version marker\nexit 0\n")
        self._install_hook(tmp_path, "post-commit", POST_COMMIT_HOOK)
        r = check_hooks_freshness(str(tmp_path))
        assert r.status == "WARN"
        assert "legacy" in r.message
        assert r.fix is not None
        assert "hooks reinstall" in r.fix

    def test_old_versioned_hook_warns(self, tmp_path):
        _real_repo(tmp_path)
        self._install_hook(tmp_path, "pre-commit",
                           "#!/bin/sh\n# schist-hook-version: 1\nexit 0\n")
        self._install_hook(tmp_path, "post-commit", POST_COMMIT_HOOK)
        r = check_hooks_freshness(str(tmp_path))
        assert r.status == "WARN"
        assert "v1" in r.message
        assert f"v{HOOK_VERSION}" in r.message

    def test_pinned_marker_silences_warning(self, tmp_path):
        """User who customized their hook can opt out with `pinned`."""
        _real_repo(tmp_path)
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

    # --- Non-dict / null JSON hardening (#437, #441) ------------------------
    # run_doctor has no per-check exception shield, so any of these valid-JSON
    # but wrong-shape settings files would otherwise abort the entire doctor
    # run with a raw traceback. Each must return a CheckResult, never raise.

    @pytest.mark.parametrize("raw", ["null", "[]", '"a-string"', "42"])
    def test_top_level_non_dict_json_returns_warn_not_crash(
        self, tmp_path, monkeypatch, raw
    ):
        """#437: a settings file whose top-level JSON isn't a dict."""
        (tmp_path / ".claude.json").write_text(raw)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(None)
        assert r.status == "WARN"
        assert "no schist entry found" in r.message

    def test_null_mcpservers_returns_warn_not_crash(self, tmp_path, monkeypatch):
        """#441 scenario A: `{"mcpServers": null}` — key present, value null."""
        (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": None}))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(None)
        assert r.status == "WARN"
        assert "no schist entry found" in r.message

    def test_null_schist_entry_returns_warn_not_crash(self, tmp_path, monkeypatch):
        """#441 scenario B: `{"mcpServers": {"schist": null}}` — null entry."""
        (tmp_path / ".claude.json").write_text(
            json.dumps({"mcpServers": {"schist": None}})
        )
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(None)
        assert r.status == "WARN"
        assert "not a JSON object" in r.message

    def test_null_non_schist_entry_returns_warn_not_crash(
        self, tmp_path, monkeypatch
    ):
        """#441 scenario B': a null value under a non-schist server name must
        not crash the fallback scan over `servers.items()`."""
        (tmp_path / ".claude.json").write_text(
            json.dumps({"mcpServers": {"other-server": None}})
        )
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(None)
        assert r.status == "WARN"
        assert "no schist entry found" in r.message

    def test_null_schist_entry_in_cursor_fallback_returns_warn(
        self, tmp_path, monkeypatch
    ):
        """The Cursor fallback path shares the `located` unpack, so a null
        schist entry there must also WARN, not crash."""
        (tmp_path / ".cursor").mkdir()
        (tmp_path / ".cursor" / "mcp.json").write_text(
            json.dumps({"mcpServers": {"schist": None}})
        )
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(None)
        assert r.status == "WARN"
        assert "not a JSON object" in r.message

    @pytest.mark.parametrize(
        "raw",
        [
            "null",
            "[]",
            '{"mcpServers": null}',
            '{"mcpServers": {"schist": null}}',
            '{"mcpServers": {"other": null}}',
        ],
    )
    def test_mcp_dist_dir_from_config_survives_bad_shapes(
        self, tmp_path, monkeypatch, raw
    ):
        """#441: the schema/vocab-alignment helper reads the same configs under
        run_doctor's unshielded loop — it must return None, never raise."""
        (tmp_path / ".claude.json").write_text(raw)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert _mcp_dist_dir_from_config(None) is None

    # --- Leaf-field type hardening (args / env inside a well-formed entry) ---
    # A dict entry can still carry a non-list `args` or non-dict `env`; the
    # container guards don't cover those, and args[0] / `for a in args` /
    # env.get would still crash the unshielded doctor loop.

    @pytest.mark.parametrize("bad_args", [42, True, {"a": 1}, "string"])
    def test_schist_entry_non_list_args_returns_warn_not_crash(
        self, tmp_path, monkeypatch, bad_args
    ):
        (tmp_path / ".claude.json").write_text(
            json.dumps({"mcpServers": {"schist": {"args": bad_args}}})
        )
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        with patch("schist.doctor._auto_detect_mcp_path", return_value=None):
            r = check_mcp_config(None)
        assert r.status == "WARN"
        assert "no args[0]" in r.message

    @pytest.mark.parametrize("bad_args", [None, 42])
    def test_non_schist_entry_non_list_args_returns_warn_not_crash(
        self, tmp_path, monkeypatch, bad_args
    ):
        """A dict cfg (passes the cfg guard) with a null/scalar args must not
        crash the `for a in args` scan — the gap the null-cfg test missed."""
        (tmp_path / ".claude.json").write_text(
            json.dumps({"mcpServers": {"other": {"args": bad_args}}})
        )
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        r = check_mcp_config(None)
        assert r.status == "WARN"
        assert "no schist entry found" in r.message

    @pytest.mark.parametrize("bad_env", ["oops", [1, 2], 42])
    def test_schist_entry_non_dict_env_returns_result_not_crash(
        self, tmp_path, monkeypatch, bad_env
    ):
        """A truthy non-dict `env` survives the old `or {}` and would crash
        env.get when a vault_path is supplied (sub-check 2)."""
        fake_mcp = tmp_path / "fake-mcp" / "dist" / "index.js"
        fake_mcp.parent.mkdir(parents=True)
        fake_mcp.write_text("// stub\n")
        (tmp_path / ".claude.json").write_text(json.dumps({
            "mcpServers": {"schist": {"args": [str(fake_mcp)], "env": bad_env}}
        }))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        with patch("schist.doctor._auto_detect_mcp_path", return_value=None):
            r = check_mcp_config(str(tmp_path / "vault"))
        # A non-dict env is treated as absent → no SCHIST_VAULT_PATH mismatch
        # warning, so the check passes on the otherwise-valid entry.
        assert r.status == "PASS"

    @pytest.mark.parametrize(
        "raw",
        [
            '{"mcpServers": {"schist": {"args": 42}}}',
            '{"mcpServers": {"schist": {"args": null}}}',
            '{"mcpServers": {"other": {"args": null}}}',
            '{"mcpServers": {"other": {"args": 42}}}',
        ],
    )
    def test_mcp_dist_dir_from_config_survives_bad_args(
        self, tmp_path, monkeypatch, raw
    ):
        (tmp_path / ".claude.json").write_text(raw)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert _mcp_dist_dir_from_config(None) is None

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
        _real_repo(tmp_path)
        (tmp_path / ".git" / "hooks").mkdir(exist_ok=True)
        # Both hooks: #536 added a pre-commit check, and this "minimal valid
        # vault" stopped being valid without one. It installed post-commit
        # only, and its asserted-label set below omitted "Pre-commit hook", so
        # the new check FAILed here and nothing noticed (#555).
        for _name in ("post-commit", "pre-commit"):
            _hook = tmp_path / ".git" / "hooks" / _name
            _hook.write_text("#!/bin/sh\n")
            _hook.chmod(0o755)  # #460: sync.py installs hooks 0o755; git skips
                                # a non-executable one, so doctor FAILs on it.
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
                        "Post-commit hook", "Pre-commit hook", "Hooks path",
                        "Ingest"}
        vault_results = [r for r in results if r.label in vault_labels]
        assert all(r.status == "PASS" for r in vault_results)
        # Registration, not just behaviour: an intersection assertion passes
        # vacuously for a check that was never run.
        assert vault_labels <= {r.label for r in results}


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

    def test_wildcard_writer_not_reported_as_lacking(self, tmp_path):
        """#512: an admin identity with write:['*'] is never a 'holder'.

        Seen live on the eleven-party hub: pi holds ['*'] but signal (b) built
        `holders` from concrete strings only, so pi was reported as lacking
        every dir any other participant held — nine false-positive lines.
        """
        from schist import hub_admin
        from schist.doctor import check_hub_acl_drift
        hub = self._make_hub(tmp_path)

        # Direct dict mutation, not participant_add: hub_admin deliberately
        # refuses to create a '*' writer (it also authorizes editing
        # vault.yaml over SSH), so a hand-seeded admin identity like the
        # production hub's pi can only be reproduced this way.
        def make_alpha_wildcard(d):
            d["access"]["alpha"]["write"] = ["*"]
            return True

        hub_admin.apply_mutation(hub, make_alpha_wildcard, "m")
        # 'logs' is infra (excluded from signal (a)), held by beta only.
        hub_admin.apply_mutation(
            hub, lambda d: hub_admin.grant_write(d, "beta", "logs"), "m")
        r = check_hub_acl_drift(str(hub))
        assert r.status == "PASS", r.message

    def test_holder_list_counts_effective_coverage(self, tmp_path):
        """#512: a wildcard writer appears in 'held by' when the WARN fires.

        Signal (b) previously listed only literal grantees, so a genuine drift
        line understated who could write the dir — the admin identity was
        missing from the very sentence an operator acts on.
        """
        from schist import hub_admin
        from schist.doctor import check_hub_acl_drift
        hub = self._make_hub(tmp_path)

        def make_alpha_wildcard(d):
            d["access"]["alpha"]["write"] = ["*"]
            return True

        hub_admin.apply_mutation(hub, make_alpha_wildcard, "m")
        # 'logs' is infra, so only signal (b) can fire for it. beta holds it
        # literally, gamma not at all, alpha via the wildcard.
        hub_admin.apply_mutation(
            hub, lambda d: hub_admin.participant_add(d, "gamma", write=["notes"]),
            "m")
        hub_admin.apply_mutation(
            hub, lambda d: hub_admin.grant_write(d, "beta", "logs"), "m")
        r = check_hub_acl_drift(str(hub))
        assert r.status == "WARN"
        assert "'logs' held by alpha, beta but not gamma" in r.message

    def test_parent_grant_not_reported_as_lacking(self, tmp_path):
        """#512: a parent grant covers a child scope, per _scope_matches."""
        from schist import hub_admin
        from schist.doctor import check_hub_acl_drift
        hub = self._make_hub(tmp_path)
        hub_admin.apply_mutation(
            hub, lambda d: hub_admin.grant_write(d, "beta", "research"), "m")
        hub_admin.apply_mutation(
            hub, lambda d: hub_admin.grant_write(d, "alpha", "research/mario"), "m")
        r = check_hub_acl_drift(str(hub))
        # beta's 'research' covers alpha's concrete 'research/mario' child, so
        # the whole check is clean — asserting on status rather than on the
        # absence of a message substring, which would pass vacuously if the
        # message format ever changed.
        assert r.status == "PASS", r.message

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
        # #556: the pre-receive check is the write ACL's only guard, and
        # nothing asserted it was WIRED IN — it could have been deleted from
        # run_doctor() entirely and this suite would have stayed green. Verify
        # a real init_hub hub PASSes it, so this covers registration AND the
        # happy path against the actually-installed hook.
        assert "Hub pre-receive hook" in labels
        pr = next(r for r in results if r.label == "Hub pre-receive hook")
        assert pr.status == "PASS", pr.message

    def test_run_doctor_omits_hub_check_without_path(self):
        from schist.doctor import run_doctor
        results = run_doctor(None, None, as_json=False)
        labels = [r.label for r in results]
        assert "Hub ACL drift" not in labels
        assert "Hub pre-receive hook" not in labels


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


class TestHubKeyPinning:
    """check_hub_key_pinning (#502)."""

    PIN_ALPHA = ('restrict,command="schist-shell alpha" '
                 'ssh-ed25519 a2V5LW1hdGVyaWFsLWE= alice')
    PIN_BETA = ('restrict,command="schist-shell beta" '
                'ssh-ed25519 a2V5LW1hdGVyaWFsLWI= bob')
    UNPINNED = "ssh-ed25519 a2V5LW1hdGVyaWFsLWM= carol"

    def _make_hub(self, tmp_path, require_pinned=False):
        import shutil
        if shutil.which("git") is None:
            pytest.skip("git not available")
        from types import SimpleNamespace
        from schist.sync import init_hub
        hub = tmp_path / "hub.git"
        init_hub(SimpleNamespace(name="v", participant=["alpha", "beta"]), str(hub))
        if require_pinned:
            from schist import hub_admin

            def enable(d):
                d["security"] = {"require_pinned_identity": True}
                return True

            hub_admin.apply_mutation(hub, enable, "enable pinning")
        return hub

    def test_skip_without_hub_path(self):
        from schist.doctor import check_hub_key_pinning
        assert check_hub_key_pinning(None).status == "SKIP"

    def test_warn_when_authorized_keys_missing(self, tmp_path):
        from schist.doctor import check_hub_key_pinning
        hub = self._make_hub(tmp_path)
        r = check_hub_key_pinning(str(hub), str(tmp_path / "nope"))
        assert r.status == "WARN"
        assert "not found" in r.message

    def test_fail_on_unpinned_key_without_enforcement(self, tmp_path):
        from schist.doctor import check_hub_key_pinning
        hub = self._make_hub(tmp_path, require_pinned=False)
        ak = tmp_path / "ak"
        ak.write_text(f"{self.PIN_ALPHA}\n{self.UNPINNED}\n")
        r = check_hub_key_pinning(str(hub), str(ak))
        assert r.status == "FAIL"
        assert "line 2" in r.message

    def test_unpinned_key_with_enforcement_is_warn(self, tmp_path):
        # With enforcement on, pre-receive rejects unpinned pushes, and an
        # unpinned shell key is the normal shape of the admin's login key.
        from schist.doctor import check_hub_key_pinning
        hub = self._make_hub(tmp_path, require_pinned=True)
        ak = tmp_path / "ak"
        ak.write_text(f"{self.PIN_ALPHA}\n{self.PIN_BETA}\n{self.UNPINNED}\n")
        r = check_hub_key_pinning(str(hub), str(ak))
        assert r.status == "WARN"
        assert "shell-capable" in r.message

    def test_warn_when_enforcement_off(self, tmp_path):
        from schist.doctor import check_hub_key_pinning
        hub = self._make_hub(tmp_path, require_pinned=False)
        ak = tmp_path / "ak"
        ak.write_text(f"{self.PIN_ALPHA}\n{self.PIN_BETA}\n")
        r = check_hub_key_pinning(str(hub), str(ak))
        assert r.status == "WARN"
        assert "require_pinned_identity is off" in r.message

    def test_warn_on_unknown_pin_and_unkeyed_participant(self, tmp_path):
        from schist.doctor import check_hub_key_pinning
        hub = self._make_hub(tmp_path, require_pinned=True)
        ak = tmp_path / "ak"
        ghost = self.PIN_ALPHA.replace("schist-shell alpha", "schist-shell ghost")
        ak.write_text(f"{ghost}\n{self.PIN_BETA}\n")
        r = check_hub_key_pinning(str(hub), str(ak))
        assert r.status == "WARN"
        assert "ghost" in r.message
        assert "alpha" in r.message  # participant with no pinned key

    def test_pass_when_all_pinned_and_enforced(self, tmp_path):
        from schist.doctor import check_hub_key_pinning
        hub = self._make_hub(tmp_path, require_pinned=True)
        ak = tmp_path / "ak"
        ak.write_text(f"{self.PIN_ALPHA}\n{self.PIN_BETA}\n")
        r = check_hub_key_pinning(str(hub), str(ak))
        assert r.status == "PASS"


class TestHubSshdAcceptEnv:
    """check_hub_sshd_acceptenv (#502)."""

    def _run(self, tmp_path, monkeypatch, text):
        from schist import doctor as doctor_mod
        cfg = tmp_path / "sshd_config"
        cfg.write_text(text)
        monkeypatch.setattr(doctor_mod, "SSHD_BINARY", "/nonexistent-sshd")
        monkeypatch.setattr(doctor_mod, "SSHD_CONFIG_PATHS", [str(cfg)])
        monkeypatch.setattr(doctor_mod, "SSHD_CONFIG_GLOB",
                            str(tmp_path / "sshd_config.d" / "*.conf"))
        return doctor_mod.check_hub_sshd_acceptenv("some-hub")

    def test_skip_without_hub_path(self):
        from schist.doctor import check_hub_sshd_acceptenv
        assert check_hub_sshd_acceptenv(None).status == "SKIP"

    def test_skip_when_unreadable(self, tmp_path, monkeypatch):
        from schist import doctor as doctor_mod
        monkeypatch.setattr(doctor_mod, "SSHD_BINARY", "/nonexistent-sshd")
        monkeypatch.setattr(doctor_mod, "SSHD_CONFIG_PATHS",
                            [str(tmp_path / "missing")])
        monkeypatch.setattr(doctor_mod, "SSHD_CONFIG_GLOB",
                            str(tmp_path / "none" / "*.conf"))
        assert doctor_mod.check_hub_sshd_acceptenv("hub").status == "SKIP"

    def test_warns_on_schist_acceptenv(self, tmp_path, monkeypatch):
        r = self._run(tmp_path, monkeypatch, "AcceptEnv LANG SCHIST_IDENTITY\n")
        assert r.status == "WARN"
        assert "SCHIST_IDENTITY" in r.message

    def test_warns_on_wildcard_acceptenv(self, tmp_path, monkeypatch):
        r = self._run(tmp_path, monkeypatch, "AcceptEnv *\n")
        assert r.status == "WARN"

    def test_warns_on_schist_glob(self, tmp_path, monkeypatch):
        r = self._run(tmp_path, monkeypatch, "acceptenv SCHIST_*\n")
        assert r.status == "WARN"

    def test_pass_on_benign_acceptenv(self, tmp_path, monkeypatch):
        r = self._run(tmp_path, monkeypatch,
                      "AcceptEnv LANG LC_ALL GIT_PROTOCOL\nPermitRootLogin no\n")
        assert r.status == "PASS"

    def test_warns_on_path_and_git_acceptenv(self, tmp_path, monkeypatch):
        r = self._run(tmp_path, monkeypatch, "AcceptEnv PATH GIT_CONFIG_GLOBAL\n")
        assert r.status == "WARN"
        assert "PATH" in r.message and "GIT_CONFIG_GLOBAL" in r.message

    def test_warns_on_ld_preload_acceptenv(self, tmp_path, monkeypatch):
        r = self._run(tmp_path, monkeypatch, "AcceptEnv LD_PRELOAD PYTHONPATH\n")
        assert r.status == "WARN"


class TestHubKeyPinningWrapperPresence:
    """#513: pinned keys with no resolvable schist-shell = every pinned push
    dies at the forced command — doctor must FAIL, not PASS."""

    PIN = ('restrict,command="schist-shell alpha" '
           'ssh-ed25519 a2V5LW1hdGVyaWFsLWE= alice')

    def _hub(self, tmp_path):
        import shutil as _shutil
        if _shutil.which("git") is None:
            pytest.skip("git not available")
        from types import SimpleNamespace
        from schist.sync import init_hub
        hub = tmp_path / "hub.git"
        init_hub(SimpleNamespace(name="v", participant=["alpha"]), str(hub))
        return hub

    def test_warn_when_wrapper_missing(self, tmp_path, monkeypatch):
        # WARN not FAIL (#524 review): doctor's PATH is neither superset nor
        # subset of sshd's forced-command PATH (~/.ssh/environment install,
        # sudo secure_path), so absence is a strong smell, not proof.
        from schist import doctor as doctor_mod
        hub = self._hub(tmp_path)
        ak = tmp_path / "ak"
        # Pinned key + enforcement on, so the ONLY signal is wrapper-missing.
        from schist import hub_admin
        hub_admin.apply_mutation(
            hub, lambda d: d.__setitem__("security", {"require_pinned_identity": True}) or True, "on")
        ak.write_text(self.PIN + "\n")
        monkeypatch.setattr(doctor_mod.shutil, "which",
                            lambda name: None if name == "schist-shell" else "/usr/bin/x")
        r = doctor_mod.check_hub_key_pinning(str(hub), str(ak))
        assert r.status == "WARN"
        assert "not on this process's PATH" in r.message

    def test_wrapper_present_and_all_pinned_passes(self, tmp_path, monkeypatch):
        from schist import doctor as doctor_mod, hub_admin
        hub = self._hub(tmp_path)
        ak = tmp_path / "ak"
        hub_admin.apply_mutation(
            hub, lambda d: d.__setitem__("security", {"require_pinned_identity": True}) or True, "on")
        ak.write_text(self.PIN + "\n")
        monkeypatch.setattr(doctor_mod.shutil, "which", lambda name: "/usr/local/bin/schist-shell")
        r = doctor_mod.check_hub_key_pinning(str(hub), str(ak))
        assert r.status == "PASS"

    def test_no_wrapper_needed_when_nothing_pinned(self, tmp_path, monkeypatch):
        from schist import doctor as doctor_mod
        hub = self._hub(tmp_path)
        ak = tmp_path / "ak"
        ak.write_text("ssh-ed25519 a2V5LW1hdGVyaWFsLWE= plain\n")
        monkeypatch.setattr(doctor_mod.shutil, "which",
                            lambda name: None if name == "schist-shell" else "/usr/bin/x")
        r = doctor_mod.check_hub_key_pinning(str(hub), str(ak))
        assert r.status != "PASS"  # unpinned key, enforcement off -> FAIL path
        assert "schist-shell forced" in r.message


class TestAcceptEnvGlobs:
    """#523: sshd AcceptEnv tokens are patterns — GIT* forwards GIT_EXEC_PATH
    just like GIT_* does."""

    @pytest.mark.parametrize("tok", [
        "GIT*", "GIT_*", "SCHIST*", "*IDENTITY", "LD*", "PYTHON*", "PA?H", "G*",
        # Prefix-family globs with no matching representative in
        # _ACCEPTENV_DANGEROUS — these slipped through when the glob branch
        # only fnmatched the representative list (#524 review finding 3).
        "LD_AU*", "GIT_T*", "GIT_O*", "GIT_D*", "GIT_N*", "DYLD_*",
        # A GIT_ glob is flagged conservatively: only the exact GIT_PROTOCOL
        # is carved out, so a broad git-family glob gets a WARN.
        "GIT_PROTO*",
    ])
    def test_dangerous_globs_flagged(self, tok):
        from schist.doctor import _acceptenv_offender
        assert _acceptenv_offender(tok) is True

    @pytest.mark.parametrize("tok", ["GIT_PROTOCOL", "LANG", "LC_*", "TERM*",
                                     "COLORTERM", "NO_COLOR"])
    def test_benign_tokens_pass(self, tok):
        from schist.doctor import _acceptenv_offender
        assert _acceptenv_offender(tok) is False


class TestCheckMcpCliSpawn:
    """#560: mcp-server/src/tools.ts spawns `schist` / `schist-ingest` BY BARE
    NAME, resolved against the spawning client's PATH. A GUI/launchd-launched
    client never reads the login shell's rc files, so a CLI in ~/.local/bin is
    unreachable and every background push dies with `spawn schist ENOENT` —
    while doctor, running in the login shell, reported everything green.
    """

    GUI_PATH_FIXTURE = "/usr/bin:/bin:/usr/sbin:/sbin"

    def _client(self, home, env=None, *, shape="mcpServers", name="schist"):
        """Write one MCP client config under a fake home; return its path."""
        stub = home / "fake-mcp" / "dist" / "index.js"
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text("// stub\n")
        entry = {"command": "node", "args": [str(stub)]}
        if env is not None:
            entry["env"] = env
        if shape == "mcpServers":
            path = home / ".claude.json"
            path.write_text(json.dumps({"mcpServers": {name: entry}}))
        else:
            # Claude Science's list-of-servers registry.
            path = home / ".claude-science" / "mcp" / "local-mcp.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            entry["name"] = name
            path.write_text(json.dumps({"servers": [entry]}))
        return path

    def _fake_bin(self, tmp_path, name):
        """An executable that exists but is NOT on the GUI PATH."""
        d = tmp_path / "localbin"
        d.mkdir(exist_ok=True)
        b = d / name
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(0o755)
        return b

    def _run(self, monkeypatch, home, gui_path=None):
        from schist.doctor import check_mcp_cli_spawn
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        monkeypatch.setattr("schist.doctor._gui_launch_path",
                            lambda: gui_path if gui_path is not None else self.GUI_PATH_FIXTURE)
        return check_mcp_cli_spawn(None)

    def test_skips_when_no_client_config(self, tmp_path, monkeypatch):
        r = self._run(monkeypatch, tmp_path)
        assert r.status == "SKIP"

    def test_fails_when_unpinned_and_not_on_gui_path(self, tmp_path, monkeypatch):
        """The production failure: bare name + GUI PATH that lacks ~/.local/bin."""
        self._client(tmp_path)
        r = self._run(monkeypatch, tmp_path)
        assert r.status == "FAIL"
        assert "SCHIST_BIN is not pinned" in r.message
        assert "SCHIST_INGEST_BIN is not pinned" in r.message

    def test_passes_when_pinned_to_absolute_paths(self, tmp_path, monkeypatch):
        """Pinning is the remedy — it must actually flip the verdict."""
        s = self._fake_bin(tmp_path, "schist")
        i = self._fake_bin(tmp_path, "schist-ingest")
        self._client(tmp_path, env={"SCHIST_BIN": str(s), "SCHIST_INGEST_BIN": str(i)})
        r = self._run(monkeypatch, tmp_path)
        assert r.status == "PASS", r.message

    def test_fails_when_pin_points_at_nothing(self, tmp_path, monkeypatch):
        """A pin to a deleted/renamed binary is a break, not a pass."""
        self._client(tmp_path, env={
            "SCHIST_BIN": str(tmp_path / "gone" / "schist"),
            "SCHIST_INGEST_BIN": str(tmp_path / "gone" / "schist-ingest"),
        })
        r = self._run(monkeypatch, tmp_path)
        assert r.status == "FAIL"
        assert "does not resolve to an executable" in r.message

    def test_fails_when_pin_exists_but_is_not_executable(self, tmp_path, monkeypatch):
        """git SKIPS a non-executable hook and continues (#545); spawn ENOENTs
        on a non-executable binary. Existence is not enough."""
        d = tmp_path / "localbin"
        d.mkdir()
        for n in ("schist", "schist-ingest"):
            (d / n).write_text("#!/bin/sh\nexit 0\n")
            (d / n).chmod(0o644)
        self._client(tmp_path, env={"SCHIST_BIN": str(d / "schist"),
                                    "SCHIST_INGEST_BIN": str(d / "schist-ingest")})
        r = self._run(monkeypatch, tmp_path)
        assert r.status == "FAIL"
        assert "does not resolve to an executable" in r.message

    def test_empty_pin_is_treated_as_unpinned(self, tmp_path, monkeypatch):
        """Parity with schistCliBin: `process.env.SCHIST_BIN?.trim() || binName`
        falls back to the bare name for empty AND whitespace values, so an
        exported-but-empty var must not be reported as a pin (#123 comment)."""
        self._client(tmp_path, env={"SCHIST_BIN": "", "SCHIST_INGEST_BIN": "   "})
        r = self._run(monkeypatch, tmp_path)
        assert r.status == "FAIL"
        assert "is not pinned" in r.message
        # Must not claim the empty string failed to resolve as a path.
        assert "does not resolve to an executable" not in r.message

    def test_passes_when_binaries_live_on_the_gui_path(self, tmp_path, monkeypatch):
        """No pin needed when the install is somewhere launchd already looks."""
        d = tmp_path / "usrlocalbin"
        d.mkdir()
        for n in ("schist", "schist-ingest"):
            (d / n).write_text("#!/bin/sh\nexit 0\n")
            (d / n).chmod(0o755)
        self._client(tmp_path)
        r = self._run(monkeypatch, tmp_path, gui_path=str(d))
        assert r.status == "PASS", r.message

    def test_discovers_claude_science_servers_list_shape(self, tmp_path, monkeypatch):
        """Claude Science's registry is `{"servers": [...]}`, not `mcpServers`,
        and was invisible to every doctor check before #560 — which is why the
        client that kept breaking was the one nothing validated."""
        self._client(tmp_path, shape="servers", name="schist-vault")
        r = self._run(monkeypatch, tmp_path)
        assert r.status == "FAIL"
        assert "schist-vault" in r.message
        assert "local-mcp.json" in r.message

    def test_reports_every_broken_client_not_just_the_first(self, tmp_path, monkeypatch):
        """check_mcp_config stops at the first entry; a per-client
        misconfiguration only shows up if every client is audited."""
        self._client(tmp_path)
        self._client(tmp_path, shape="servers", name="schist-vault")
        r = self._run(monkeypatch, tmp_path)
        assert r.status == "FAIL"
        assert "2 MCP client entries" in r.message

    def test_malformed_configs_do_not_raise(self, tmp_path, monkeypatch):
        """run_doctor has no per-check exception shield (#437, #441): a
        null/list/scalar config must degrade, never abort the whole run."""
        (tmp_path / ".claude.json").write_text("null")
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").write_text("[1, 2, 3]")
        (tmp_path / ".claude-science" / "mcp").mkdir(parents=True)
        (tmp_path / ".claude-science" / "mcp" / "local-mcp.json").write_text(
            json.dumps({"servers": [None, {"name": "schist-vault"}]}))
        r = self._run(monkeypatch, tmp_path)
        assert r.status in ("SKIP", "FAIL", "PASS")

    def test_remedy_never_names_an_ephemeral_uv_path(self, tmp_path, monkeypatch):
        """The remedy is written into a config by hand, so it must name a path
        that survives: doctor is routinely run under `uv run --with ./cli`,
        whose PATH prepends ~/.cache/uv/archive-v0/<hash>/bin. Pinning that is
        a config that breaks at the next cache eviction."""
        from schist.doctor import _stable_cli_path
        ephemeral = tmp_path / ".cache" / "uv" / "archive-v0" / "abc" / "bin"
        ephemeral.mkdir(parents=True)
        b = ephemeral / "schist"
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(0o755)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr("shutil.which", lambda n, path=None: str(b) if n == "schist" else None)
        assert _stable_cli_path("schist") is None

    def test_stable_path_prefers_local_bin(self, tmp_path, monkeypatch):
        from schist.doctor import _stable_cli_path
        lb = tmp_path / ".local" / "bin"
        lb.mkdir(parents=True)
        b = lb / "schist"
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(0o755)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert _stable_cli_path("schist") == str(b)

    def test_wired_into_run_doctor(self, tmp_path, monkeypatch):
        """#556's lesson: a check nothing asserts is REGISTERED can be deleted
        from run_doctor() with the suite green. Assert the label is present and
        carries a real verdict, not merely that no expected label is missing."""
        from schist.doctor import run_doctor
        self._client(tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr("schist.doctor._gui_launch_path", lambda: self.GUI_PATH_FIXTURE)
        results = run_doctor(None, None, as_json=False)
        labels = [r.label for r in results]
        assert "MCP CLI spawn" in labels
        r = next(x for x in results if x.label == "MCP CLI spawn")
        assert r.status == "FAIL", r.message
