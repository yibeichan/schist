# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the git pre-receive hook ACL enforcement."""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from schist.acl import VaultACL, parse_vault_data
from schist.pre_receive import (
    FRONTMATTER_SCOPE,
    Violation,
    check_push,
    derive_scope,
    format_rejection,
    log_rejection,
    main,
    pinning_rejection,
    resolve_identity,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

VAULT_DATA = {
    "name": "test-vault",
    "vault_version": 1,
    "scope_convention": "subdirectory",
    "participants": [
        {"name": "admin", "type": "agent"},
        {"name": "cluster-mario", "type": "spoke", "transport": "git-only"},
        {"name": "cluster-hbcd", "type": "spoke", "transport": "git-only"},
        {"name": "researcher", "type": "agent"},
        {"name": "narrow-writer", "type": "agent"},
    ],
    "access": {
        "admin": {"read": ["*"], "write": ["*"]},
        "cluster-mario": {"read": ["*"], "write": ["research/mario"]},
        "cluster-hbcd": {"read": ["*"], "write": ["research/hbcd"]},
        "researcher": {"read": ["*"], "write": ["research"]},
        "narrow-writer": {"read": ["*"], "write": ["ops"]},
    },
}


@pytest.fixture()
def acl() -> VaultACL:
    return parse_vault_data(VAULT_DATA)


# ---------------------------------------------------------------------------
# derive_scope
# ---------------------------------------------------------------------------


class TestDeriveScope:
    def test_nested_file(self):
        assert derive_scope("research/mario/2026-04-01-findings.md") == "research/mario"

    def test_single_dir(self):
        assert derive_scope("research/note.md") == "research"

    def test_deeply_nested(self):
        assert derive_scope("a/b/c/d/file.txt") == "a/b/c/d"

    def test_root_level_file(self):
        assert derive_scope("vault.yaml") == ""

    def test_root_level_readme(self):
        assert derive_scope("README.md") == ""

    def test_dir_with_dotfile(self):
        assert derive_scope("ops/.gitkeep") == "ops"


# ---------------------------------------------------------------------------
# resolve_identity
# ---------------------------------------------------------------------------


class TestResolveIdentity:
    def test_schist_identity_takes_priority(self):
        with patch.dict("os.environ", {"SCHIST_IDENTITY": "cluster-mario", "GL_USER": "gitolite-user"}):
            assert resolve_identity() == "cluster-mario"

    def test_gl_user_fallback(self):
        env = {"GL_USER": "cluster-hbcd"}
        with patch.dict("os.environ", env, clear=True):
            assert resolve_identity() == "cluster-hbcd"

    def test_none_when_no_env(self):
        with patch.dict("os.environ", {}, clear=True):
            assert resolve_identity() is None

    def test_empty_schist_identity_falls_through(self):
        with patch.dict("os.environ", {"SCHIST_IDENTITY": "", "GL_USER": "fallback"}):
            assert resolve_identity() == "fallback"


# ---------------------------------------------------------------------------
# check_push — core ACL enforcement
# ---------------------------------------------------------------------------


class TestCheckPush:
    def test_in_scope_write_allowed(self, acl):
        files = ["research/mario/note.md", "research/mario/sub/deep.md"]
        violations = check_push("cluster-mario", files, acl, "refs/heads/main")
        assert violations == []

    def test_out_of_scope_write_rejected(self, acl):
        files = ["security/secrets.md"]
        violations = check_push("cluster-mario", files, acl, "refs/heads/main")
        assert len(violations) == 1
        assert violations[0].filepath == "security/secrets.md"
        assert violations[0].scope == "security"
        assert violations[0].identity == "cluster-mario"

    def test_mixed_in_and_out_of_scope(self, acl):
        files = [
            "research/mario/ok.md",
            "security/bad.md",
            "ops/also-bad.md",
        ]
        violations = check_push("cluster-mario", files, acl, "refs/heads/main")
        assert len(violations) == 2
        bad_files = {v.filepath for v in violations}
        assert bad_files == {"security/bad.md", "ops/also-bad.md"}

    def test_admin_wildcard_allows_everything(self, acl):
        files = [
            "research/mario/note.md",
            "security/audit.md",
            "ops/deploy.md",
            "vault.yaml",  # root-level
        ]
        violations = check_push("admin", files, acl, "refs/heads/main")
        assert violations == []

    def test_root_file_rejected_for_non_wildcard(self, acl):
        files = ["vault.yaml"]
        violations = check_push("cluster-mario", files, acl, "refs/heads/main")
        assert len(violations) == 1
        assert violations[0].scope == "(root)"

    def test_root_file_allowed_for_wildcard_writer(self, acl):
        files = ["vault.yaml", "README.md"]
        violations = check_push("admin", files, acl, "refs/heads/main")
        assert violations == []

    def test_parent_scope_grants_child_write(self, acl):
        """researcher has write:[research] — should cover research/mario/."""
        files = ["research/mario/note.md", "research/hbcd/data.md"]
        violations = check_push("researcher", files, acl, "refs/heads/main")
        assert violations == []

    def test_child_scope_does_not_grant_parent(self, acl):
        """cluster-mario has write:[research/mario] — cannot write research/."""
        files = ["research/top-level-note.md"]
        violations = check_push("cluster-mario", files, acl, "refs/heads/main")
        assert len(violations) == 1
        assert violations[0].scope == "research"

    def test_sibling_scope_rejected(self, acl):
        """cluster-mario cannot write research/hbcd/."""
        files = ["research/hbcd/stolen.md"]
        violations = check_push("cluster-mario", files, acl, "refs/heads/main")
        assert len(violations) == 1

    def test_empty_file_list(self, acl):
        violations = check_push("cluster-mario", [], acl, "refs/heads/main")
        assert violations == []

    def test_similar_prefix_no_false_match(self, acl):
        """research-extra/ should NOT match scope 'research'."""
        files = ["research-extra/note.md"]
        violations = check_push("researcher", files, acl, "refs/heads/main")
        assert len(violations) == 1

    def test_refname_preserved_in_violation(self, acl):
        files = ["security/bad.md"]
        violations = check_push("cluster-mario", files, acl, "refs/heads/feature")
        assert violations[0].refname == "refs/heads/feature"


# ---------------------------------------------------------------------------
# format_rejection
# ---------------------------------------------------------------------------


class TestFormatRejection:
    def test_single_violation(self):
        violations = [Violation("cluster-mario", "security/bad.md", "security", "refs/heads/main")]
        msg = format_rejection(violations)
        assert "REJECTED" in msg
        assert "cluster-mario" in msg
        assert "security/bad.md" in msg

    def test_multiple_violations(self):
        violations = [
            Violation("cluster-mario", "security/a.md", "security", "refs/heads/main"),
            Violation("cluster-mario", "ops/b.md", "ops", "refs/heads/main"),
        ]
        msg = format_rejection(violations)
        assert "security/a.md" in msg
        assert "ops/b.md" in msg

    def test_includes_scope_in_parens(self):
        violations = [Violation("x", "a/b.md", "a", "refs/heads/main")]
        msg = format_rejection(violations)
        assert "(scope: a)" in msg


# ---------------------------------------------------------------------------
# log_rejection
# ---------------------------------------------------------------------------


class TestLogRejection:
    def test_creates_log_entry(self, tmp_path):
        log_path = tmp_path / "rejected-pushes.log"
        violations = [
            Violation("cluster-mario", "security/bad.md", "security", "refs/heads/main"),
        ]
        log_rejection(violations, log_path=log_path)

        content = log_path.read_text()
        assert "REJECTED" in content
        assert "cluster-mario" in content
        assert "security/bad.md" in content
        assert "refs/heads/main" in content

    def test_appends_to_existing_log(self, tmp_path):
        log_path = tmp_path / "rejected-pushes.log"
        log_path.write_text("[previous entry]\n")

        violations = [Violation("x", "a.md", "a", "refs/heads/main")]
        log_rejection(violations, log_path=log_path)

        content = log_path.read_text()
        assert "[previous entry]" in content
        assert "REJECTED" in content

    def test_creates_parent_dirs(self, tmp_path):
        log_path = tmp_path / "deep" / "nested" / "rejected-pushes.log"
        violations = [Violation("x", "a.md", "a", "refs/heads/main")]
        log_rejection(violations, log_path=log_path)
        assert log_path.exists()

    def test_multiple_files_in_entry(self, tmp_path):
        log_path = tmp_path / "rejected-pushes.log"
        violations = [
            Violation("mario", "a.md", "a", "refs/heads/main"),
            Violation("mario", "b.md", "b", "refs/heads/main"),
        ]
        log_rejection(violations, log_path=log_path)
        content = log_path.read_text()
        assert "a.md, b.md" in content

    def test_iso_timestamp_in_log(self, tmp_path):
        log_path = tmp_path / "rejected-pushes.log"
        violations = [Violation("x", "a.md", "a", "refs/heads/main")]
        log_rejection(violations, log_path=log_path)
        content = log_path.read_text()
        # Should contain ISO 8601 timestamp with timezone
        assert "+00:00" in content or "Z" in content


# ---------------------------------------------------------------------------
# main() integration — mocked git subprocess
# ---------------------------------------------------------------------------


class TestMain:
    """Integration tests for the main() entry point with mocked git."""

    @pytest.fixture(autouse=True)
    def _isolate_rate_limit_db(self, tmp_path, monkeypatch):
        """Point rate-limit sqlite at a per-test tmp dir.

        Tests in this class exercise main() without passing db_path, so
        the rate-limit check falls back to `$GIT_DIR/rate-limits.sqlite`.
        Setting GIT_DIR for the duration of the test keeps the DB inside
        tmp_path and avoids polluting the repo root.
        """
        monkeypatch.setenv("GIT_DIR", str(tmp_path))

    def _run(
        self,
        acl: VaultACL,
        identity: str,
        stdin_lines: list[str],
        changed_files: list[str],
        log_path: Path | None = None,
    ) -> int:
        """Helper to run main() with mocked get_changed_files."""
        with patch("schist.pre_receive.get_changed_files", return_value=changed_files):
            return main(
                stdin=stdin_lines,
                acl=acl,
                identity=identity,
                log_path=log_path,
            )

    def test_allow_in_scope_push(self, acl):
        stdin = ["abc123 def456 refs/heads/main"]
        rc = self._run(acl, "cluster-mario", stdin, ["research/mario/note.md"])
        assert rc == 0

    def test_reject_out_of_scope_push(self, acl, tmp_path, capsys):
        log_path = tmp_path / "rejected-pushes.log"
        stdin = ["abc123 def456 refs/heads/main"]
        rc = self._run(
            acl, "cluster-mario", stdin,
            ["security/bad.md"],
            log_path=log_path,
        )
        assert rc == 1
        stderr = capsys.readouterr().err
        assert "REJECTED" in stderr
        assert log_path.exists()

    def test_no_identity_rejects(self, acl, capsys):
        rc = main(stdin=["abc def refs/heads/main"], acl=acl, identity=None)
        # identity=None and no env vars → should reject
        with patch.dict("os.environ", {}, clear=True):
            rc = main(stdin=["abc def refs/heads/main"], acl=acl)
        assert rc == 1
        stderr = capsys.readouterr().err
        assert "cannot determine push identity" in stderr

    def test_unknown_identity_rejects(self, acl, capsys):
        stdin = ["abc123 def456 refs/heads/main"]
        with patch("schist.pre_receive.get_changed_files", return_value=[]):
            rc = main(stdin=stdin, acl=acl, identity="unknown-agent")
        assert rc == 1
        stderr = capsys.readouterr().err
        assert "unknown identity" in stderr

    def test_no_vault_yaml_allows_push(self):
        """If no vault.yaml exists, push is allowed (vault.yaml is optional)."""
        with patch("schist.pre_receive.load_acl", return_value=None):
            rc = main(
                stdin=["abc def refs/heads/main"],
                identity="anyone",
            )
        assert rc == 0

    def test_git_log_timeout_rejects_with_human_message(self, acl, capsys):
        """#243: a TimeoutExpired from get_changed_files rejects fail-safe with a
        readable message, not an uncaught traceback leaked to the pusher."""
        stdin = ["abc123 def456 refs/heads/main"]
        timeout = subprocess.TimeoutExpired(cmd=["git", "log"], timeout=30)
        with patch("schist.pre_receive.get_changed_files", side_effect=timeout):
            rc = main(stdin=stdin, acl=acl, identity="cluster-mario")
        assert rc == 1
        stderr = capsys.readouterr().err
        assert "timed out" in stderr
        assert "Traceback" not in stderr

    def test_empty_stdin(self, acl):
        rc = self._run(acl, "admin", [""], [])
        assert rc == 0

    def test_multiple_refs(self, acl, tmp_path):
        """Multiple ref updates in a single push: a '*' writer may update a
        second branch; a scoped writer may update only the served one."""
        stdin = [
            "aaa bbb refs/heads/main",
            "ccc ddd refs/heads/feature",
        ]
        # Both refs change only in-scope files
        with patch("schist.pre_receive.get_changed_files", return_value=["research/mario/note.md"]):
            assert main(stdin=stdin, acl=acl, identity="admin") == 0
            assert main(stdin=stdin, acl=acl, identity="cluster-mario") == 1

    def test_multiple_refs_one_bad(self, acl, tmp_path, capsys):
        """One ref is fine, another has out-of-scope files."""
        log_path = tmp_path / "rejected-pushes.log"
        call_count = [0]
        files_per_call = [
            ["research/mario/ok.md"],
            ["security/bad.md"],
        ]

        def mock_changed_files(oldrev, newrev, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return files_per_call[idx]

        stdin = [
            "aaa bbb refs/heads/main",
            "ccc ddd refs/heads/feature",
        ]
        with patch("schist.pre_receive.get_changed_files", side_effect=mock_changed_files):
            rc = main(stdin=stdin, acl=acl, identity="admin", log_path=log_path)
        assert rc == 0
        assert call_count[0] == 2
        call_count[0] = 0
        with patch("schist.pre_receive.get_changed_files", side_effect=mock_changed_files):
            rc = main(stdin=stdin, acl=acl, identity="researcher", log_path=log_path)
        assert rc == 1

    def test_deleted_branch_needs_a_wildcard_writer(self, acl):
        """Deleting a branch (newrev=0*40) is allowed for a '*' writer only."""
        zero = "0" * 40
        stdin = [f"abc123 {zero} refs/heads/old-branch"]
        with patch("schist.pre_receive.get_changed_files", return_value=[]):
            assert main(stdin=stdin, acl=acl, identity="admin") == 0
            assert main(stdin=stdin, acl=acl, identity="cluster-mario") == 1

    def test_admin_can_push_anything(self, acl):
        stdin = ["abc def refs/heads/main"]
        files = [
            "vault.yaml",
            "research/mario/a.md",
            "security/audit.md",
            "ops/deploy.yaml",
        ]
        rc = self._run(acl, "admin", stdin, files)
        assert rc == 0

    def test_narrow_writer_blocked_outside_scope(self, acl, capsys):
        stdin = ["abc def refs/heads/main"]
        files = ["research/mario/note.md"]
        rc = self._run(acl, "narrow-writer", stdin, files)
        assert rc == 1

    def test_narrow_writer_allowed_in_scope(self, acl):
        stdin = ["abc def refs/heads/main"]
        files = ["ops/status.md", "ops/sub/deep.md"]
        rc = self._run(acl, "narrow-writer", stdin, files)
        assert rc == 0


# ---------------------------------------------------------------------------
# get_changed_files — subprocess mocking
# ---------------------------------------------------------------------------


class TestGetChangedFilesRealGit:
    """#703: shapes of change the plain `git log --name-only` listing missed,
    checked against real repositories. A rename was listed by its destination
    only, and a merge commit listed nothing, so a path that exists only in the
    merge result (or was deleted only there) was never checked."""

    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")
        self._write("mine/a.md", "a\n")
        self._write("theirs/b.md", "b\n")
        self._git("add", ".")
        self._git("commit", "-qm", "base")
        return repo

    @staticmethod
    def _git(*args: str) -> str:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()

    @staticmethod
    def _write(rel: str, content: str) -> None:
        p = Path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def _commit(self, message: str) -> str:
        self._git("add", "-A")
        self._git("commit", "-qm", message)
        return self._git("rev-parse", "HEAD")

    def _hub_side(self, base: str, rel: str) -> str:
        """Another participant's commit on top of `base`, already on the hub."""
        self._git("switch", "-q", "-c", "hub", base)
        self._write(rel, "theirs\n")
        tip = self._commit("other participant")
        self._git("switch", "-q", "main")
        return tip

    def test_rename_lists_the_source_path_too(self, repo):
        from schist.pre_receive import get_changed_files

        base = self._git("rev-parse", "HEAD")
        self._git("mv", "theirs/b.md", "mine/b.md")
        tip = self._commit("move it")

        assert set(get_changed_files(base, tip)) == {"theirs/b.md", "mine/b.md"}

    def test_clean_pull_merge_does_not_list_the_other_parents_files(self, repo):
        """A routine merge of work already on the hub must not be rejected for
        paths in another participant's scope that it merely brought in."""
        from schist.pre_receive import get_changed_files

        base = self._git("rev-parse", "HEAD")
        hub = self._hub_side(base, "theirs/new.md")
        self._write("mine/w.md", "w\n")
        self._commit("mine")
        self._git("merge", "-q", "--no-edit", "hub")
        tip = self._git("rev-parse", "HEAD")

        assert get_changed_files(hub, tip) == ["mine/w.md"]

    def test_file_added_only_in_the_merge_result_is_listed(self, repo):
        from schist.pre_receive import get_changed_files

        base = self._git("rev-parse", "HEAD")
        hub = self._hub_side(base, "theirs/new.md")
        self._write("mine/w.md", "w\n")
        self._commit("mine")
        self._git("merge", "-q", "--no-commit", "hub")
        self._write("theirs/evil.md", "added by the merge\n")
        tip = self._commit("merge")

        assert set(get_changed_files(hub, tip)) == {"mine/w.md", "theirs/evil.md"}

    def test_file_deleted_only_in_the_merge_result_is_listed(self, repo):
        from schist.pre_receive import get_changed_files

        base = self._git("rev-parse", "HEAD")
        hub = self._hub_side(base, "theirs/new.md")
        self._write("mine/w.md", "w\n")
        self._commit("mine")
        self._git("merge", "-q", "--no-commit", "hub")
        self._git("rm", "-q", "theirs/b.md")
        tip = self._commit("merge")

        assert set(get_changed_files(hub, tip)) == {"mine/w.md", "theirs/b.md"}

    def test_octopus_merge_lists_only_what_differs_from_every_parent(self, repo):
        from schist.pre_receive import get_changed_files

        base = self._git("rev-parse", "HEAD")
        hub = self._hub_side(base, "theirs/new.md")
        self._git("switch", "-q", "-c", "hub2", base)
        self._write("theirs/other.md", "x\n")
        hub2 = self._commit("third participant")
        self._git("switch", "-q", "main")
        self._write("mine/w.md", "w\n")
        self._commit("mine")
        self._git("merge", "-q", "--no-commit", "hub", "hub2")
        self._write("theirs/evil.md", "added by the octopus\n")
        tip = self._commit("octopus")

        # The range hub..tip also contains hub2's own commit (theirs/other.md);
        # the merge itself must add only the path it introduced.
        assert set(get_changed_files(hub, tip)) - {"theirs/other.md"} == {"mine/w.md", "theirs/evil.md"}
        assert hub2

    def test_merge_whose_tree_is_an_old_hub_commit_lists_what_it_reverts(self, repo):
        """A merge lists only paths that differ from EVERY parent, and the
        pusher chooses the parents: with an old hub commit as one parent and
        its tree as the result, every later change to another scope is rolled
        back while the combined diff lists nothing. The net diff from the
        branch's current tip sees it."""
        from schist.pre_receive import get_changed_files

        old = self._git("rev-parse", "HEAD")
        self._write("theirs/b.md", "their later edit\n")
        self._write("theirs/new.md", "theirs\n")
        hub = self._commit("other participant, already on the hub")
        old_tree = self._git("rev-parse", f"{old}^{{tree}}")
        rollback = self._git("commit-tree", "-p", hub, "-p", old, "-m", "merge", old_tree)

        assert {"theirs/b.md", "theirs/new.md"} <= set(get_changed_files(hub, rollback))

    def test_ordinary_merge_that_reverts_and_drops_hub_side_files_lists_them(self, repo):
        from schist.pre_receive import get_changed_files

        base = self._git("rev-parse", "HEAD")
        self._write("theirs/b.md", "their later edit\n")
        self._write("theirs/new.md", "theirs\n")
        hub = self._commit("other participant, already on the hub")
        self._git("switch", "-q", "-c", "side", base)
        self._write("mine/w.md", "w\n")
        self._commit("mine")
        self._git("switch", "-q", "main")
        self._git("merge", "-q", "--no-ff", "--no-commit", "side")
        self._git("checkout", base, "--", "theirs/b.md")
        self._git("rm", "-q", "theirs/new.md")
        tip = self._commit("merge")

        assert {"mine/w.md", "theirs/b.md", "theirs/new.md"} <= set(get_changed_files(hub, tip))

    def test_merge_of_two_new_commits_cannot_hide_a_rollback(self, repo):
        """No parent of the merge is on the hub, so a per-parent check has
        nothing to compare with; the net diff still does."""
        from schist.pre_receive import get_changed_files

        old = self._git("rev-parse", "HEAD")
        self._write("theirs/b.md", "their later edit\n")
        hub = self._commit("other participant, already on the hub")
        self._write("mine/one.md", "1\n")
        new1 = self._commit("mine, on the hub tip")
        self._git("switch", "-q", "-c", "stale", old)
        self._write("mine/two.md", "2\n")
        new2 = self._commit("mine, on an old base")
        self._git("switch", "-q", "main")
        tree = self._git("rev-parse", f"{new2}^{{tree}}")
        merge = self._git("commit-tree", "-p", new1, "-p", new2, "-m", "merge", tree)

        assert "theirs/b.md" in get_changed_files(hub, merge)

    def test_new_branch_form_lists_rename_sources_too(self, repo):
        from schist.pre_receive import ZERO_SHA, get_changed_files

        self._git("switch", "-q", "-c", "feature")
        self._git("mv", "theirs/b.md", "mine/b.md")
        tip = self._commit("move it")
        self._git("switch", "-q", "main")
        self._git("branch", "-D", "feature")

        assert set(get_changed_files(ZERO_SHA, tip)) == {"theirs/b.md", "mine/b.md"}


class TestNonUtf8VaultYaml:
    def test_a_vault_yaml_that_is_not_utf8_rejects_the_push(self, tmp_path, monkeypatch, capsys):
        """#709 listed `extract_vault_yaml_from_git`'s strict decode as a
        traceback risk. main() already turns any load failure into a REJECTED
        line, and that is the behavior to keep: returning None from the
        extractor instead would read as "no vault.yaml" and ALLOW the push."""
        from schist.pre_receive import main

        repo = tmp_path / "hub.git"
        work = tmp_path / "work"
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        # A VALID ACL apart from one byte that is not UTF-8, so the rejection
        # can only come from the decode, not from ACL validation.
        (work / "vault.yaml").write_bytes(
            b"name: test-vault\nvault_version: 1\nscope_convention: subdirectory\n"
            b"participants:\n- name: someone\n  type: agent\n"
            b"access:\n  someone:\n    read: ['*']\n    write: ['*']\n"
            b"# caf\xe9\n"
        )
        for args in (["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
                     ["add", "vault.yaml"], ["commit", "-qm", "acl"]):
            subprocess.run(["git", *args], cwd=work, check=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(work), str(repo)], check=True)
        monkeypatch.chdir(repo)
        monkeypatch.setenv("GIT_DIR", str(repo))

        rc = main(stdin=["abc def refs/heads/main"], identity="someone")

        assert rc == 1
        err = capsys.readouterr().err
        assert "REJECTED: failed to load vault.yaml" in err
        assert "codec can't decode" in err
        assert "Traceback" not in err


class TestGetChangedFiles:
    def test_deleted_branch_returns_empty(self):
        from schist.pre_receive import ZERO_SHA, get_changed_files

        result = get_changed_files("abc123", ZERO_SHA)
        assert result == []

    def test_existing_branch_uses_commit_history_not_net_diff(self):
        from schist.pre_receive import get_changed_files

        from schist.pre_receive import _SCAN_GIT as _PIN
        outputs = iter([[b"a.md", b"b.md", b"a.md", b""], [b"c.md", b"a.md", b""]])
        cmds = []

        def fake(cmd, timeout):
            cmds.append(cmd)
            return iter(next(outputs))

        with patch("schist.pre_receive._git_nul_tokens", side_effect=fake):
            result = get_changed_files("aaa", "bbb")
        # Per-commit listing first, then the net diff, de-duplicated (#703).
        assert result == ["a.md", "b.md", "c.md"]
        assert cmds == [
            [*_PIN, "log", "--root",
             "--name-only", "--no-renames", "-c", "--format=", "-z", "aaa..bbb"],
            [*_PIN, "diff", "--name-only", "--no-renames", "-z", "aaa", "bbb"],
        ]

    def test_new_branch_uses_commit_history_not_tip_diff(self):
        from schist.pre_receive import ZERO_SHA, get_changed_files

        from schist.pre_receive import _SCAN_GIT as _PIN
        with patch("schist.pre_receive._git_nul_tokens",
                   return_value=iter([b"new-file.md", b""])) as mock_tokens:
            result = get_changed_files(ZERO_SHA, "abc123")
        assert result == ["new-file.md"]
        cmd = mock_tokens.call_args[0][0]
        assert cmd == [
            *_PIN, "log", "--root",
            "--name-only", "--no-renames", "-c", "--format=", "-z",
            "abc123", "--not", "--all",
        ]

    def test_empty_diff_returns_empty(self):
        from schist.pre_receive import get_changed_files

        with patch("schist.pre_receive._git_nul_tokens", side_effect=lambda c, t: iter([])):
            result = get_changed_files("aaa", "bbb")
        assert result == []

    @pytest.mark.integration
    def test_intermediate_reverted_file_is_rejected(self, acl, tmp_path, monkeypatch, capsys):
        """ACL checks must inspect every pushed commit, not only the net diff.

        The simulated push starts at ``oldrev``. The first new commit writes an
        out-of-scope file; the second removes it, so ``git diff oldrev newrev``
        would not show that path. The pre-receive hook must still reject the
        push because the unauthorized content entered git history.
        """
        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*args: str) -> str:
            result = subprocess.run(
                ["git", *args],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()

        git("init")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test User")

        allowed = repo / "research" / "mario" / "ok.md"
        allowed.parent.mkdir(parents=True)
        allowed.write_text("ok\n")
        git("add", "research/mario/ok.md")
        git("commit", "-m", "base")
        oldrev = git("rev-parse", "HEAD")

        bad = repo / "security" / "bad.md"
        bad.parent.mkdir(parents=True)
        bad.write_text("bad\n")
        git("add", "security/bad.md")
        git("commit", "-m", "bad intermediate")

        bad.unlink()
        git("add", "security/bad.md")
        git("commit", "-m", "revert bad")
        newrev = git("rev-parse", "HEAD")

        monkeypatch.chdir(repo)
        rc = main(
            stdin=[f"{oldrev} {newrev} refs/heads/main"],
            acl=acl,
            identity="cluster-mario",
            log_path=tmp_path / "rejected-pushes.log",
            db_path=tmp_path / "rate-limits.sqlite",
        )

        assert rc == 1
        stderr = capsys.readouterr().err
        assert "out-of-scope writes" in stderr
        assert "security/bad.md" in stderr


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_file_in_scope_subdir_with_dots(self, acl):
        """Files with dots in directory names should work."""
        files = ["research/mario/2026.04.01/note.md"]
        violations = check_push("cluster-mario", files, acl, "refs/heads/main")
        assert violations == []

    def test_gitkeep_in_scope(self, acl):
        files = ["research/mario/.gitkeep"]
        violations = check_push("cluster-mario", files, acl, "refs/heads/main")
        assert violations == []

    def test_path_traversal_attempt(self, acl):
        """Path traversal in file path is checked against derived scope."""
        # derive_scope normalizes via Path, but the scope check should still work
        files = ["research/mario/../hbcd/stolen.md"]
        violations = check_push("cluster-mario", files, acl, "refs/heads/main")
        # Path("research/mario/../hbcd/stolen.md").parent resolves to "research/hbcd"
        # cluster-mario cannot write research/hbcd
        assert len(violations) == 1

    def test_vault_yaml_load_failure_rejects(self, capsys):
        """If vault.yaml fails to parse, push is rejected."""
        with patch("schist.pre_receive.load_acl", side_effect=Exception("parse error")):
            rc = main(stdin=["abc def refs/heads/main"], identity="anyone")
        assert rc == 1
        stderr = capsys.readouterr().err
        assert "failed to load vault.yaml" in stderr


# ---------------------------------------------------------------------------
# main() rate-limit integration
# ---------------------------------------------------------------------------


# require_pinned_identity is pinned False rather than left to the parser
# default: main() runs the pinning gate BEFORE the rate limiter, and
# log_pinning_rejection also emits src=, so if that default ever flips the
# src= tests below would keep passing while asserting nothing about the rate
# limiter's own log line.
RATE_LIMIT_VAULT_DATA = {
    "name": "rl-test",
    "vault_version": 1,
    "scope_convention": "subdirectory",
    "participants": [
        {"name": "admin", "type": "agent"},
        {"name": "other", "type": "agent"},
    ],
    "access": {
        "admin": {"read": ["*"], "write": ["*"]},
        "other": {"read": ["*"], "write": ["ops"]},
    },
    "rate_limits": {
        "admin": {"git_syncs_per_hour": 2, "notes_per_sync": 3},
    },
    "security": {"require_pinned_identity": False},
}


@pytest.fixture()
def rl_acl() -> VaultACL:
    return parse_vault_data(RATE_LIMIT_VAULT_DATA)


class TestMainRateLimit:
    def _run(
        self,
        acl: VaultACL,
        identity: str,
        stdin_lines: list[str],
        changed_files: list[str],
        *,
        log_path: Path,
        db_path: Path,
    ) -> int:
        with patch("schist.pre_receive.get_changed_files", return_value=changed_files):
            return main(
                stdin=stdin_lines,
                acl=acl,
                identity=identity,
                log_path=log_path,
                db_path=db_path,
            )

    def test_rate_limit_rejection_path(self, rl_acl, tmp_path, capsys):
        """ACL passes, rate limit trips after exhausting git_syncs_per_hour."""
        log_path = tmp_path / "rejected-pushes.log"
        db_path = tmp_path / "rate-limits.sqlite"
        stdin = ["abc def refs/heads/main"]
        files = ["notes/a.md"]

        # Admin's limit is 2. Pushes 1 and 2 should pass; push 3 should reject.
        for _ in range(2):
            rc = self._run(
                rl_acl, "admin", stdin, files,
                log_path=log_path, db_path=db_path,
            )
            assert rc == 0
        rc = self._run(
            rl_acl, "admin", stdin, files,
            log_path=log_path, db_path=db_path,
        )
        assert rc == 1
        stderr = capsys.readouterr().err
        assert "rate limit exceeded" in stderr
        assert "git_syncs_per_hour" in stderr
        assert "Retry after" in stderr
        # Rejection logged with identity tag.
        content = log_path.read_text()
        assert "RATE_LIMIT_REJECTED" in content
        assert "identity=admin" in content

    def test_rate_limit_log_carries_src(self, rl_acl, tmp_path, monkeypatch):
        """#526: log_rate_limit_rejection must record the client IP too.

        Four functions write this log; TestPushSource covers two of them, and
        this path had only identity/reason assertions — so dropping src= from
        its format string would have gone unnoticed. (The fourth,
        rate_limit._fail_open, still logs neither identity nor src: #528.)
        """
        monkeypatch.setenv("SSH_CONNECTION", "203.0.113.1 51000 10.0.0.1 22")
        log_path = tmp_path / "rejected-pushes.log"
        db_path = tmp_path / "rate-limits.sqlite"
        stdin = ["abc def refs/heads/main"]
        files = ["notes/a.md"]

        for _ in range(2):  # admin's git_syncs_per_hour is 2
            assert self._run(rl_acl, "admin", stdin, files,
                             log_path=log_path, db_path=db_path) == 0
        assert self._run(rl_acl, "admin", stdin, files,
                         log_path=log_path, db_path=db_path) == 1

        content = log_path.read_text()
        assert "RATE_LIMIT_REJECTED" in content
        assert "src=203.0.113.1" in content

    def test_rate_limit_bypass_log_carries_identity_and_src(
        self, rl_acl, tmp_path, monkeypatch,
    ):
        """#528: fail-open audit entries must remain attributable."""
        monkeypatch.setenv("SSH_CONNECTION", "203.0.113.1 51000 10.0.0.1 22")
        log_path = tmp_path / "rejected-pushes.log"
        db_path = tmp_path / "rate-limits.sqlite"
        db_path.write_bytes(b"not a sqlite db" * 100)

        assert self._run(
            rl_acl, "admin", ["abc def refs/heads/main"], ["notes/a.md"],
            log_path=log_path, db_path=db_path,
        ) == 0

        content = log_path.read_text()
        assert "RATE_LIMIT_BYPASSED" in content
        assert "identity=admin" in content
        assert "src=203.0.113.1" in content

    def test_rate_limit_log_src_local_without_ssh(self, rl_acl, tmp_path,
                                                  monkeypatch):
        """A filesystem-local push logs src=local, not an empty field."""
        monkeypatch.delenv("SSH_CONNECTION", raising=False)
        monkeypatch.delenv("SSH_CLIENT", raising=False)
        log_path = tmp_path / "rejected-pushes.log"
        db_path = tmp_path / "rate-limits.sqlite"
        stdin = ["abc def refs/heads/main"]
        files = ["notes/a.md"]

        for _ in range(2):
            assert self._run(rl_acl, "admin", stdin, files,
                             log_path=log_path, db_path=db_path) == 0
        assert self._run(rl_acl, "admin", stdin, files,
                         log_path=log_path, db_path=db_path) == 1

        content = log_path.read_text()
        assert "RATE_LIMIT_REJECTED" in content
        assert "src=local" in content

    def test_rate_limit_passes_under_limit(self, rl_acl, tmp_path):
        """ACL passes, rate limit passes, exit 0."""
        log_path = tmp_path / "rejected-pushes.log"
        db_path = tmp_path / "rate-limits.sqlite"
        rc = self._run(
            rl_acl, "admin",
            ["abc def refs/heads/main"],
            ["notes/a.md"],
            log_path=log_path, db_path=db_path,
        )
        assert rc == 0
        # No rejection log written on success.
        assert not log_path.exists()

    def test_notes_per_sync_rejection_path(self, rl_acl, tmp_path, capsys):
        """A single push with more notes than notes_per_sync is rejected."""
        log_path = tmp_path / "rejected-pushes.log"
        db_path = tmp_path / "rate-limits.sqlite"
        # Admin limit is 3 notes per sync; push 4.
        files = [f"notes/{i}.md" for i in range(4)]
        rc = self._run(
            rl_acl, "admin",
            ["abc def refs/heads/main"],
            files,
            log_path=log_path, db_path=db_path,
        )
        assert rc == 1
        stderr = capsys.readouterr().err
        assert "rate limit exceeded" in stderr
        assert "notes_per_sync" in stderr
        # The DB should NOT have recorded this attempt (notes_per_sync is
        # stateless and runs before the sqlite transaction).
        import sqlite3
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                (n,) = conn.execute(
                    "SELECT COUNT(*) FROM sync_events WHERE identity = 'admin'"
                ).fetchone()
            except sqlite3.OperationalError:
                n = 0
            conn.close()
            assert n == 0

    def test_rate_limit_runs_after_acl(self, rl_acl, tmp_path, capsys):
        """ACL violation wins over rate-limit violation.

        'other' cannot write to notes/, AND pushing 4 notes would trip
        notes_per_sync. ACL runs first so the rejection reason is ACL,
        and no rate-limit slot is consumed.
        """
        log_path = tmp_path / "rejected-pushes.log"
        db_path = tmp_path / "rate-limits.sqlite"
        files = [f"notes/{i}.md" for i in range(4)]
        rc = self._run(
            rl_acl, "other",
            ["abc def refs/heads/main"],
            files,
            log_path=log_path, db_path=db_path,
        )
        assert rc == 1
        stderr = capsys.readouterr().err
        # ACL rejection surfaces (out-of-scope), not rate-limit rejection.
        assert "out-of-scope writes" in stderr
        assert "rate limit exceeded" not in stderr
        # DB file should not exist — rate_limit.check_rate_limit was never called.
        assert not db_path.exists()

    def test_multi_ref_push_deduplicates_notes(self, rl_acl, tmp_path):
        """A multi-ref push touching the same file across refs counts it once.

        Regression guard: ``notes_per_sync`` is a count of unique
        note-bearing files in a push. A legitimate push to two branches
        that share files must not be double-counted.
        """
        log_path = tmp_path / "rejected-pushes.log"
        db_path = tmp_path / "rate-limits.sqlite"
        stdin = [
            "aaa bbb refs/heads/main",
            "ccc ddd refs/heads/feature",
        ]
        # Admin limit is 3 notes_per_sync. We push 3 unique files across
        # two refs — 6 total file-touches, but only 3 unique files. Without
        # dedup this would be rejected as 6 > 3.
        shared_files = ["notes/a.md", "notes/b.md", "notes/c.md"]

        with patch(
            "schist.pre_receive.get_changed_files",
            return_value=shared_files,
        ):
            rc = main(
                stdin=stdin,
                acl=rl_acl,
                identity="admin",
                log_path=log_path,
                db_path=db_path,
            )
        assert rc == 0


# ---------------------------------------------------------------------------
# pinned-identity enforcement (#502)
# ---------------------------------------------------------------------------


def _pinned_vault_data():
    import copy
    d = copy.deepcopy(VAULT_DATA)
    d["security"] = {"require_pinned_identity": True}
    return d


@pytest.fixture()
def pinned_acl() -> VaultACL:
    return parse_vault_data(_pinned_vault_data())


class TestPinningRejection:
    def test_disabled_allows_anything(self, acl):
        env = {"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}
        assert pinning_rejection(acl, env) is None

    def test_ssh_push_without_pin_rejected(self, pinned_acl):
        env = {"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22", "SCHIST_IDENTITY": "admin"}
        msg = pinning_rejection(pinned_acl, env)
        assert msg is not None
        assert "require_pinned_identity" in msg

    def test_ssh_client_var_also_counts_as_ssh(self, pinned_acl):
        msg = pinning_rejection(pinned_acl, {"SSH_CLIENT": "1.2.3.4 5 22"})
        assert msg is not None

    def test_pinned_ssh_push_allowed(self, pinned_acl):
        env = {"SSH_CONNECTION": "x", "SCHIST_IDENTITY_PINNED": "1"}
        assert pinning_rejection(pinned_acl, env) is None

    def test_wrong_pin_value_rejected(self, pinned_acl):
        env = {"SSH_CONNECTION": "x", "SCHIST_IDENTITY_PINNED": "yes"}
        assert pinning_rejection(pinned_acl, env) is not None

    def test_local_push_exempt(self, pinned_acl):
        # No SSH_* env => local push; filesystem access is admin authority
        # (same trust model as hub_admin.py).
        assert pinning_rejection(pinned_acl, {"SCHIST_IDENTITY": "admin"}) is None


class TestMainPinning:
    @pytest.fixture(autouse=True)
    def _isolate_rate_limit_db(self, tmp_path, monkeypatch):
        """Point rate-limit sqlite at a per-test tmp dir (#525).

        The allow-path tests below reach check_rate_limit without a db_path,
        so without this the DB lands in the CWD — which is how a binary DB
        got committed to the repo root. Same fixture as TestMain.
        """
        monkeypatch.setenv("GIT_DIR", str(tmp_path))

    def test_main_rejects_unpinned_ssh_push(self, pinned_acl, tmp_path, capsys,
                                            monkeypatch):
        monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
        monkeypatch.delenv("SCHIST_IDENTITY_PINNED", raising=False)
        log = tmp_path / "rejected.log"
        rc = main(stdin=["old new refs/heads/main"], acl=pinned_acl,
                  identity="admin", log_path=log)
        assert rc == 1
        assert "require_pinned_identity" in capsys.readouterr().err
        assert "PINNING_REJECTED identity=admin" in log.read_text()

    def test_main_allows_pinned_ssh_push(self, pinned_acl, tmp_path, monkeypatch):
        monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
        monkeypatch.setenv("SCHIST_IDENTITY_PINNED", "1")
        with patch("schist.pre_receive.get_changed_files",
                   return_value=["research/mario/note.md"]):
            rc = main(stdin=["old new refs/heads/main"], acl=pinned_acl,
                      identity="cluster-mario", log_path=tmp_path / "r.log")
        assert rc == 0

    def test_main_allows_local_push_without_pin(self, pinned_acl, tmp_path,
                                                monkeypatch):
        monkeypatch.delenv("SSH_CONNECTION", raising=False)
        monkeypatch.delenv("SSH_CLIENT", raising=False)
        monkeypatch.delenv("SCHIST_IDENTITY_PINNED", raising=False)
        with patch("schist.pre_receive.get_changed_files",
                   return_value=["research/mario/note.md"]):
            rc = main(stdin=["old new refs/heads/main"], acl=pinned_acl,
                      identity="cluster-mario", log_path=tmp_path / "r.log")
        assert rc == 0

    def test_main_unpinned_rejection_precedes_participant_check(
            self, pinned_acl, tmp_path, capsys, monkeypatch):
        # A spoofed unknown identity must hit the pinning wall, not leak the
        # participant-list error.
        monkeypatch.setenv("SSH_CONNECTION", "x")
        monkeypatch.delenv("SCHIST_IDENTITY_PINNED", raising=False)
        rc = main(stdin=[], acl=pinned_acl, identity="mallory",
                  log_path=tmp_path / "r.log")
        assert rc == 1
        assert "require_pinned_identity" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# audit-log source attribution (#518)
# ---------------------------------------------------------------------------


class TestPushSource:
    def test_ssh_connection_ip(self):
        from schist.pre_receive import _push_source
        env = {"SSH_CONNECTION": "203.0.113.9 51000 10.0.0.1 22"}
        assert _push_source(env) == "203.0.113.9"

    def test_ssh_client_fallback(self):
        from schist.pre_receive import _push_source
        assert _push_source({"SSH_CLIENT": "198.51.100.7 51000 22"}) == "198.51.100.7"

    def test_local_when_no_ssh_env(self):
        from schist.pre_receive import _push_source
        assert _push_source({}) == "local"
        assert _push_source({"SSH_CONNECTION": "  "}) == "local"

    def test_rejection_log_carries_src(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SSH_CONNECTION", "203.0.113.9 51000 10.0.0.1 22")
        log = tmp_path / "rejected.log"
        v = Violation(identity="mallory", filepath="x.md", scope="(root)",
                      refname="refs/heads/main")
        log_rejection([v], log_path=log)
        assert "src=203.0.113.9" in log.read_text()

    def test_pinning_log_carries_src(self, tmp_path, monkeypatch):
        from schist.pre_receive import log_pinning_rejection
        monkeypatch.delenv("SSH_CONNECTION", raising=False)
        monkeypatch.delenv("SSH_CLIENT", raising=False)
        log = tmp_path / "rejected.log"
        log_pinning_rejection("mallory", log_path=log)
        assert "src=local" in log.read_text()



class TestLogSanitization:
    """#524 review: identity on the pinning path is raw client input (logged
    before NAME_RE validation); filenames come from `git log -z`. A newline in
    either forges audit-log lines. _sanitize_log_field neutralizes them."""

    def test_sanitize_escapes_control_chars(self):
        from schist.pre_receive import _sanitize_log_field
        out = _sanitize_log_field("x\n[fake] PINNING_REJECTED identity=victim")
        assert "\n" not in out
        assert "\\x0a" in out
        assert _sanitize_log_field("normal-id") == "normal-id"

    def test_pinning_log_sanitizes_forged_identity(self, tmp_path):
        from schist.pre_receive import log_pinning_rejection
        log = tmp_path / "r.log"
        log_pinning_rejection(
            "evil\n[2099-01-01T00:00:00+00:00] PINNING_REJECTED identity=victim src=1.2.3.4",
            log_path=log)
        # The forged newline is escaped, so the whole attempt stays on ONE
        # physical line — no second parseable entry.
        lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "\\x0a" in lines[0]

    def test_rejection_log_sanitizes_filename(self, tmp_path):
        log = tmp_path / "r.log"
        v = Violation(identity="mallory",
                      filepath="ok.md\n[forged] REJECTED identity=x",
                      scope="(root)", refname="refs/heads/main")
        log_rejection([v], log_path=log)
        lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "\\x0a" in lines[0]


# ── #697: a filename that is not valid UTF-8 ───────────────────────────────

def _repo_with_non_utf8_path(tmp_path):
    """A real repo whose second commit adds `research/caf\\xe9.md` (latin-1).

    Built with `git fast-import`: APFS refuses to create such a name on disk,
    but git itself stores arbitrary bytes, and a Linux checkout can commit one.
    """
    import subprocess

    repo = tmp_path / "hub.git"
    subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
    stream = (
        b"commit refs/heads/main\n"
        b"committer t <t@example.com> 1700000000 +0000\n"
        b"data 4\nseed\n"
        b"M 100644 inline research/seed.md\ndata 2\nx\n\n"
        b"commit refs/heads/main\n"
        b"committer t <t@example.com> 1700000001 +0000\n"
        b"data 4\nbad\n"
        b"M 100644 inline research/caf\xe9.md\ndata 2\ny\n\n"
    )
    subprocess.run(["git", "fast-import", "--quiet"], cwd=repo, input=stream, check=True)
    shas = subprocess.run(["git", "rev-parse", "main~1", "main"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.split()
    return repo, shas[0], shas[1]


def test_get_changed_files_returns_a_non_utf8_name_instead_of_raising(tmp_path, monkeypatch):
    from schist.pre_receive import get_changed_files

    repo, old, new = _repo_with_non_utf8_path(tmp_path)
    monkeypatch.chdir(repo)
    files = get_changed_files(old, new)
    assert [f.encode("utf-8", "surrogateescape") for f in files] == [b"research/caf\xe9.md"]


def test_check_push_rejects_a_non_utf8_path_even_for_a_wildcard_writer(acl):
    name = b"research/mario/caf\xe9.md".decode("utf-8", "surrogateescape")
    for identity in ("cluster-mario", "admin"):
        violations = check_push(identity, [name], acl, "refs/heads/main")
        assert [v.scope for v in violations] == ["(path is not valid UTF-8)"], identity


def test_rejection_message_and_audit_log_show_undecodable_bytes_as_escapes(tmp_path):
    """The audit log is opened as UTF-8 and only OSError is caught, so a raw
    surrogate would raise UnicodeEncodeError out of the hook."""
    from schist.pre_receive import Violation, format_rejection, log_rejection

    v = Violation("mario", b"research/caf\xe9.md".decode("utf-8", "surrogateescape"),
                  "(path is not valid UTF-8)", "refs/heads/main")
    assert "research/caf\\xe9.md" in format_rejection([v])
    log = tmp_path / "rejected.log"
    log_rejection([v], log_path=log)
    assert "research/caf\\xe9.md" in log.read_text()


def test_main_rejects_a_pushed_non_utf8_path_with_a_message_not_a_traceback(
        acl, tmp_path, monkeypatch, capsys):
    """End to end through main() against a real repo: the hook returns 1, the
    rejection names the path with escaped bytes, and the audit log is written."""
    repo, old, new = _repo_with_non_utf8_path(tmp_path)
    monkeypatch.chdir(repo)
    log = tmp_path / "rejected.log"

    rc = main(stdin=[f"{old} {new} refs/heads/main"], acl=acl,
              identity="admin", log_path=log)

    assert rc == 1
    err = capsys.readouterr().err
    assert "research/caf\\xe9.md" in err and "not valid UTF-8" in err
    assert "research/caf\\xe9.md" in log.read_text()


def test_check_push_accepts_a_valid_non_ascii_path_in_scope(acl):
    """The guard is about bytes that are not UTF-8, not about non-ASCII."""
    assert check_push("cluster-mario", ["research/mario/café.md", "research/mario/笔记.md"],
                      acl, "refs/heads/main") == []


def test_check_push_reports_a_bad_path_once_and_still_checks_the_others(acl):
    """Dropping the `continue` would run the bad path through derive_scope as
    well and report it twice."""
    # Out of the identity's scope too, so without the `continue` it would be
    # reported a second time by the ordinary ACL check.
    bad = b"security/caf\xe9.md".decode("utf-8", "surrogateescape")
    violations = check_push("cluster-mario", [bad, "security/bad.md"], acl, "refs/heads/main")
    assert [(v.filepath, v.scope) for v in violations] == [
        (bad, "(path is not valid UTF-8)"), ("security/bad.md", "security")]


def test_rejection_for_a_bad_path_says_rename_not_edit_vault_yaml():
    from schist.pre_receive import NON_UTF8_SCOPE

    bad = b"research/caf\xe9.md".decode("utf-8", "surrogateescape")
    only_bad = format_rejection([Violation("admin", bad, NON_UTF8_SCOPE, "refs/heads/main")])
    assert "rename the file" in only_bad and "Check vault.yaml" not in only_bad
    mixed = format_rejection([
        Violation("admin", bad, NON_UTF8_SCOPE, "refs/heads/main"),
        Violation("admin", "security/x.md", "security", "refs/heads/main"),
    ])
    assert "rename the file" in mixed and "Check vault.yaml" in mixed


def test_get_changed_files_decodes_as_utf8_whatever_the_locale(tmp_path, monkeypatch):
    """sshd forwards the client's LANG/LC_*. os.fsdecode follows the locale, so
    under latin-1 a bad byte would become mojibake that check_push cannot see.
    Simulate that locale and require the surrogate anyway."""
    from schist.pre_receive import get_changed_files

    repo, old, new = _repo_with_non_utf8_path(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(os, "fsdecode", lambda b: b.decode("latin-1") if isinstance(b, bytes) else b)
    (name,) = get_changed_files(old, new)
    assert name == "research/caf\udce9.md"


# ── Frontmatter openers and ref policy ──────────────────────────────────────

def _bare_repo_with(tmp_path, commits):
    """A bare repo whose `main` gets one commit per entry of `commits`, each a
    dict path -> bytes (None deletes). Returns (repo, [sha per commit])."""
    repo = tmp_path / "hub.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(repo)], check=True)
    stream = b""
    for i, files in enumerate(commits):
        stream += (b"commit refs/heads/main\n"
                   b"committer t <t@example.com> %d +0000\n" % (1700000000 + i)
                   + b"data 2\nc\n")
        for path, data in files.items():
            if data is None:
                stream += b"D " + path.encode() + b"\n"
            else:
                stream += (b"M 100644 inline " + path.encode() + b"\n"
                           + b"data %d\n" % len(data) + data + b"\n")
        stream += b"\n"
    subprocess.run(["git", "fast-import", "--quiet"], cwd=repo, input=stream, check=True)
    shas = subprocess.run(["git", "rev-list", "--reverse", "main"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.split()
    return repo, shas


_GIT_WITHOUT_Z = r"""#!/bin/sh
# git before 2.42 has no `cat-file -Z`; parse-options rejects it.
case " $* " in
  *" cat-file "*" -Z "*|*" cat-file "*" -Z") echo "error: unknown switch \`Z'" >&2; exit 129;;
esac
exec "$REAL_GIT" "$@"
"""


@pytest.fixture(params=["git", "git-before-2.42"])
def git_version(request, tmp_path_factory, monkeypatch):
    """Run the test with the installed git, and again with a stand-in that
    rejects options git 2.30 to 2.41 do not have."""
    if request.param == "git-before-2.42":
        import shutil
        bindir = tmp_path_factory.mktemp("old-git-bin")
        stand_in = bindir / "git"
        stand_in.write_text(_GIT_WITHOUT_Z, encoding="utf-8")
        stand_in.chmod(0o755)
        monkeypatch.setenv("REAL_GIT", shutil.which("git"))
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        assert subprocess.run(["git", "cat-file", "--batch-check", "-Z"], input=b"",
                              capture_output=True).returncode == 129
    return request.param


def _peak_rss_mb(ru_maxrss: int, platform: str = sys.platform) -> float:
    """getrusage's ru_maxrss in MB: bytes on macOS, KiB on Linux."""
    return ru_maxrss / (1 << 20) if platform == "darwin" else ru_maxrss / 1024


@pytest.fixture(autouse=True)
def _skip_blob_reads(request, monkeypatch):
    """Most tests here feed main() made-up revisions with a patched file list;
    the frontmatter check reads real blobs, so it runs only where marked."""
    if request.node.get_closest_marker("reads_blobs") is None:
        monkeypatch.setattr("schist.pre_receive.find_bad_frontmatter", lambda *a, **k: [])


SEED = {"research/mario/seed.md": b"---\ntitle: seed\n---\n"}


def _opener_parity_cases() -> list[dict]:
    fixture = Path(__file__).resolve().parents[2] / "schema" / "frontmatter-opener-parity.json"
    return json.loads(fixture.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _opener_parity_cases(), ids=lambda c: c["name"])
def test_bad_opener_matches_the_shared_corpus(case):
    """schema/frontmatter-opener-parity.json pins _bad_opener against the MCP
    server's hasNonYamlOpener, which mirrors it."""
    from schist.pre_receive import _bad_opener
    assert _bad_opener(case["text"].encode("utf-8")) is case["refused"]


def test_opener_corpus_covers_both_verdicts():
    cases = _opener_parity_cases()
    assert sum(c["refused"] for c in cases) >= 10
    assert sum(not c["refused"] for c in cases) >= 10


@pytest.mark.reads_blobs
class TestFrontmatterOpeners:
    @pytest.mark.parametrize("opener", [
        b"---js", b"---javascript", b"--- js", b"---JS", b"\xef\xbb\xbf---js", b"---coffee", b"---json",
        b"---yaml",   # harmless to gray-matter, but notes open with a plain --- line
    ])
    def test_a_non_yaml_opener_is_found(self, tmp_path, monkeypatch, opener):
        from schist.pre_receive import find_bad_frontmatter, get_changed_files
        repo, (old, new) = _bare_repo_with(tmp_path, [SEED, {
            "research/mario/x.md": opener + b"\n{ title: 'x' }\n---\nbody\n"}])
        monkeypatch.chdir(repo)
        assert find_bad_frontmatter(old, new, get_changed_files(old, new)) == ["research/mario/x.md"]

    @pytest.mark.parametrize("content", [
        b"---\ntitle: ok\n---\n", b"---   \ntitle: ok\n---\n",
        b"----\nnot frontmatter\n", b"plain text\n", b"",
    ])
    def test_yaml_and_plain_notes_pass(self, tmp_path, monkeypatch, content):
        from schist.pre_receive import find_bad_frontmatter, get_changed_files
        repo, (old, new) = _bare_repo_with(tmp_path, [SEED, {"research/mario/x.md": content}])
        monkeypatch.chdir(repo)
        assert find_bad_frontmatter(old, new, get_changed_files(old, new)) == []

    def test_a_note_added_then_removed_inside_the_push_is_still_found(self, tmp_path, monkeypatch):
        from schist.pre_receive import find_bad_frontmatter, get_changed_files
        repo, (old, _mid, new) = _bare_repo_with(tmp_path, [
            SEED, {"research/mario/x.md": b"---js\n{}\n---\n"}, {"research/mario/x.md": None}])
        monkeypatch.chdir(repo)
        assert find_bad_frontmatter(old, new, get_changed_files(old, new)) == ["research/mario/x.md"]

    def test_non_markdown_files_are_not_read(self, tmp_path, monkeypatch):
        from schist.pre_receive import find_bad_frontmatter, get_changed_files
        repo, (old, new) = _bare_repo_with(tmp_path, [SEED, {"research/mario/x.txt": b"---js\n"}])
        monkeypatch.chdir(repo)
        assert find_bad_frontmatter(old, new, get_changed_files(old, new)) == []

    def test_main_rejects_the_push_for_every_identity(self, acl, tmp_path, monkeypatch, capsys):
        repo, (old, new) = _bare_repo_with(tmp_path, [SEED, {
            "research/mario/x.md": b"---js\n{ title: 'x' }\n---\n"}])
        monkeypatch.chdir(repo)
        for identity in ("cluster-mario", "admin"):
            rc = main(stdin=[f"{old} {new} refs/heads/main"], acl=acl, identity=identity,
                      log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
            assert rc == 1, identity
            assert "research/mario/x.md" in capsys.readouterr().err


    def test_each_pushed_version_is_read_once(self, tmp_path, monkeypatch):
        """Reading every changed path at every pushed commit cost commits x
        paths reads (900 here); each pushed note version is read once."""
        from schist.pre_receive import find_bad_frontmatter, get_changed_files
        commits = [SEED] + [{f"research/mario/n{i}.md": b"---\ntitle: n\n---\n" + bytes([65 + i])}
                            for i in range(30)]
        repo, shas = _bare_repo_with(tmp_path, commits)
        monkeypatch.chdir(repo)
        import schist.pre_receive as pr
        real_heads, reads = pr._blob_heads, []

        def spy(ids, timeout):
            reads.append(len(ids))
            return real_heads(ids, timeout)

        monkeypatch.setattr(pr, "_blob_heads", spy)
        assert find_bad_frontmatter(shas[0], shas[-1], get_changed_files(shas[0], shas[-1])) == []
        assert reads == [30]

    def test_a_push_that_fixes_an_old_note_is_accepted(self, tmp_path, monkeypatch):
        """Only versions the push writes are read: the note's old version,
        which an earlier hook let in, does not refuse the push fixing it."""
        from schist.pre_receive import find_bad_frontmatter, get_changed_files
        repo, (base, _c1, c2) = _bare_repo_with(tmp_path, [
            {"research/mario/x.md": b"---js\n{}\n---\n"},
            {"research/mario/other.md": b"o\n"},
            {"research/mario/x.md": b"---\ntitle: x\n---\n"}])
        monkeypatch.chdir(repo)
        assert find_bad_frontmatter(base, c2, get_changed_files(base, c2)) == []

    def test_a_note_written_only_by_a_merge_is_found(self, tmp_path, monkeypatch):
        from schist.pre_receive import find_bad_frontmatter, get_changed_files
        work = tmp_path / "w"
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        (work / "seed.md").write_bytes(b"---\ntitle: s\n---\n")
        subprocess.run(git + ["add", "."], cwd=work, check=True)
        subprocess.run(git + ["commit", "-qm", "seed"], cwd=work, check=True)
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True,
                              text=True, encoding="utf-8", check=True).stdout.strip()
        subprocess.run(git + ["checkout", "-qb", "side"], cwd=work, check=True)
        (work / "a.md").write_bytes(b"---\ntitle: a\n---\n")
        subprocess.run(git + ["add", "."], cwd=work, check=True)
        subprocess.run(git + ["commit", "-qm", "side"], cwd=work, check=True)
        subprocess.run(git + ["checkout", "-q", "main"], cwd=work, check=True)
        (work / "b.md").write_bytes(b"b\n")
        subprocess.run(git + ["add", "."], cwd=work, check=True)
        subprocess.run(git + ["commit", "-qm", "main"], cwd=work, check=True)
        subprocess.run(git + ["merge", "-q", "--no-commit", "side"], cwd=work, check=True)
        (work / "a.md").write_bytes(b"---js\n{}\n---\n")     # written by the merge alone
        subprocess.run(git + ["add", "."], cwd=work, check=True)
        subprocess.run(git + ["commit", "-qm", "merge"], cwd=work, check=True)
        new = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True,
                             text=True, encoding="utf-8", check=True).stdout.strip()
        monkeypatch.chdir(work)
        assert find_bad_frontmatter(base, new, get_changed_files(base, new)) == ["a.md"]

    @pytest.mark.parametrize("form", ["existing", "new"])
    def test_a_note_a_merge_writes_and_a_later_commit_fixes_is_found(self, tmp_path,
                                                                     monkeypatch, form):
        """The bad version is in neither the tip nor any single-parent
        commit's diff, only in the merge's diff against its parents, so the
        scan's `-m` is what finds it."""
        from schist.pre_receive import ZERO_SHA, find_bad_frontmatter
        work, hub = tmp_path / "w", tmp_path / "hub.git"
        git = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@example.com"]

        def commit(path, data, message):
            (work / path).write_bytes(data)
            subprocess.run(git + ["add", path], check=True)
            subprocess.run(git + ["commit", "-qm", message], check=True)

        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        commit("seed.md", b"---\ntitle: s\n---\n", "seed")
        base = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
        subprocess.run(git + ["checkout", "-qb", "side"], check=True)
        commit("a.md", b"---\ntitle: a\n---\n", "side")
        subprocess.run(git + ["checkout", "-q", "main"], check=True)
        commit("b.md", b"b\n", "main")
        subprocess.run(git + ["merge", "-q", "--no-commit", "side"], check=True)
        commit("a.md", b"---js\n{}\n---\n", "the merge writes a bad a.md")
        commit("a.md", b"---\ntitle: a\n---\n", "fix a.md")
        new = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True,
                             check=True).stdout.strip()
        # As in quarantine: the pushed commits are present but unreferenced.
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(hub)], check=True)
        subprocess.run(git + ["push", "-q", str(hub), f"{new}:refs/tmp/x",
                              f"{base}:refs/heads/main"], check=True)
        subprocess.run(["git", "update-ref", "-d", "refs/tmp/x"], cwd=hub, check=True)
        monkeypatch.chdir(hub)
        old = base if form == "existing" else ZERO_SHA
        assert find_bad_frontmatter(old, new, []) == ["a.md"]

    def test_an_unreadable_pushed_note_refuses_instead_of_skipping(self, tmp_path, monkeypatch):
        from schist.pre_receive import find_bad_frontmatter, get_changed_files
        repo, (old, new) = _bare_repo_with(tmp_path, [SEED, {
            "research/mario/x.md": b"---js\n{}\n---\n"}])
        _loosen(repo)
        _drop_object(repo, "HEAD:research/mario/x.md")
        monkeypatch.chdir(repo)
        with pytest.raises(subprocess.CalledProcessError):
            find_bad_frontmatter(old, new, get_changed_files(old, new))

    def test_a_force_push_back_onto_an_old_bad_note_is_found(self, tmp_path, monkeypatch):
        """A commit whose parent is an old hub commit still holding a bad
        note: the note is in no pushed commit's own diff, but lands on the
        branch, so the net diff from the old tip is read too."""
        from schist.pre_receive import find_bad_frontmatter, get_changed_files
        repo, (old_bad, tip) = _bare_repo_with(tmp_path, [
            {"research/mario/x.md": b"---js\n{}\n---\n"},
            {"research/mario/x.md": b"---\ntitle: x\n---\n"}])
        rewound = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit-tree",
             f"{old_bad}^{{tree}}", "-p", old_bad, "-m", "rewind"],
            cwd=repo, capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()
        monkeypatch.chdir(repo)
        assert find_bad_frontmatter(tip, rewound, get_changed_files(tip, rewound)) == [
            "research/mario/x.md"]

    def test_a_root_commit_is_read_whatever_the_hub_git_config(self, tmp_path, monkeypatch):
        """`log.showRoot=false` in the hub user's git config hid a root
        commit's files from `git log`."""
        from schist.pre_receive import ZERO_SHA, find_bad_frontmatter, get_changed_files
        repo, _ = _bare_repo_with(tmp_path, [SEED])
        stream = (b"commit refs/heads/orphan\ncommitter t <t@example.com> 1700000100 +0000\n"
                  b"data 2\nc\nM 100644 inline r.md\ndata 9\n---js\n{}\n\n\n")
        subprocess.run(["git", "fast-import", "--quiet"], cwd=repo, input=stream, check=True)
        root = subprocess.run(["git", "rev-parse", "orphan"], cwd=repo, capture_output=True,
                              text=True, encoding="utf-8", check=True).stdout.strip()
        subprocess.run(["git", "update-ref", "-d", "refs/heads/orphan"], cwd=repo, check=True)
        home = tmp_path / "home"
        home.mkdir()
        (home / ".gitconfig").write_text("[log]\n\tshowRoot = false\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        # conftest points GIT_CONFIG_GLOBAL at an empty file; this test needs
        # the hub user's config read.
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
        monkeypatch.chdir(repo)
        assert get_changed_files(ZERO_SHA, root) == ["r.md"]
        assert find_bad_frontmatter(ZERO_SHA, root, ["r.md"]) == ["r.md"]

    def test_a_push_listing_too_many_versions_is_refused(self, tmp_path, monkeypatch, capsys):
        import schist.pre_receive as pr
        commits = [SEED] + [{f"research/mario/n{i}.md": b"n%d\n" % i} for i in range(10)]
        repo, shas = _bare_repo_with(tmp_path, commits)
        monkeypatch.chdir(repo)
        monkeypatch.setattr(pr, "SCAN_ENTRY_LIMIT", 5)
        with pytest.raises(pr.ScanLimitExceeded):
            pr.find_bad_frontmatter(shas[0], shas[-1], [])
        from schist.acl import parse_vault_data
        acl = parse_vault_data({
            "name": "v", "vault_version": 1, "scope_convention": "subdirectory",
            "participants": [{"name": "admin", "type": "agent"}],
            "access": {"admin": {"read": ["*"], "write": ["*"]}}})
        rc = pr.main(stdin=[f"{shas[0]} {shas[-1]} refs/heads/main"], acl=acl, identity="admin",
                     log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        assert "REJECTED: push is too large for the hub to check" in capsys.readouterr().err

    def test_one_blob_at_many_paths_is_listed_in_linear_time(self, tmp_path, monkeypatch):
        """Each blob's paths were kept in a list with a membership test, so
        one blob at every path of a large tree cost paths squared: the two-tree
        attack took the whole 60 s timeout. Linear, this takes well under a
        second."""
        import time
        from schist.pre_receive import _pushed_markdown_blobs
        repo, (old,) = _bare_repo_with(tmp_path, [SEED])
        git = ["git", "-C", str(repo)]
        blob = subprocess.run(git + ["hash-object", "-w", "--stdin"], input=b"n\n",
                              capture_output=True, check=True).stdout.strip().decode()
        tree = subprocess.run(git + ["mktree"], capture_output=True, check=True, input="".join(
            f"100644 blob {blob}\tn{i}.md\n" for i in range(40_000)).encode()).stdout.strip().decode()
        new = subprocess.run(git + ["-c", "user.name=t", "-c", "user.email=t@example.com",
                                    "commit-tree", tree, "-p", old, "-m", "wide"],
                             capture_output=True, check=True).stdout.strip().decode()
        monkeypatch.chdir(repo)
        start = time.monotonic()
        blobs = _pushed_markdown_blobs(old, new)
        assert len(blobs[blob]) == 40_000
        assert time.monotonic() - start < 8

    def test_every_listing_pins_the_git_config_it_depends_on(self, tmp_path, monkeypatch):
        """Each of --root and the -c pins is enough alone for a root commit,
        so the behavioural test above cannot see one of them go missing:
        assert both on every listing command."""
        import schist.pre_receive as pr
        repo, (old, new) = _bare_repo_with(tmp_path, [SEED, {"research/mario/x.md": b"x\n"}])
        monkeypatch.chdir(repo)
        real_popen, cmds = subprocess.Popen, []

        def spy(cmd, *a, **k):
            cmds.append(list(cmd))
            return real_popen(cmd, *a, **k)

        monkeypatch.setattr(subprocess, "Popen", spy)
        pr.get_changed_files(old, new)
        pr.get_changed_files(pr.ZERO_SHA, new)
        pr._pushed_markdown_blobs(old, new)
        pr._pushed_markdown_blobs(pr.ZERO_SHA, new)
        listings = [c for c in cmds if any(v in c for v in ("log", "diff", "diff-tree"))]
        assert len(listings) == 6
        for cmd in listings:
            assert cmd[:len(pr._SCAN_GIT)] == pr._SCAN_GIT, cmd
            for pin in ("log.showRoot=true", "log.diffMerges=separate", "log.showSignature=false",
                        "diff.ignoreSubmodules=none"):
                assert pin in cmd, (pin, cmd)
            if "log" in cmd:
                assert "--root" in cmd, cmd

    @pytest.mark.parametrize("where", ["global", "hub", "gitmodules"])
    def test_gitlinks_are_listed_whatever_the_hub_git_config(self, tmp_path, monkeypatch, where):
        """`diff.ignoreSubmodules=all` in the hub user's config dropped gitlinks
        from the scope listing, so a scoped writer could add one at any path.
        A hub's `submodule.<name>.ignore` and a `.gitmodules` asking the same
        are covered too."""
        from schist.pre_receive import ZERO_SHA, get_changed_files
        repo, (old,) = _bare_repo_with(tmp_path, [{
            "research/mario/x.md": b"x\n",
            ".gitmodules": (b'[submodule "sub"]\n\tpath = other/sub\n\turl = x\n'
                            b'\tignore = all\n')}])
        git = ["git", "-C", str(repo)]
        base = subprocess.run(git + ["ls-tree", "-z", old], capture_output=True,
                              check=True).stdout
        fake = "1" * 40
        oth = subprocess.run(git + ["mktree"], input=f"160000 commit {fake}\tsub\n".encode(),
                             capture_output=True, check=True).stdout.strip().decode()
        res = subprocess.run(git + ["mktree"], capture_output=True, check=True, input=(
            f"040000 tree " + subprocess.run(
                git + ["mktree"], input=f"160000 commit {fake}\tx.md\n".encode(),
                capture_output=True, check=True).stdout.strip().decode() + "\tmario\n").encode()
        ).stdout.strip().decode()
        entries = [e for e in base.split(b"\0") if e and not e.endswith(b"\tresearch")]
        tree = subprocess.run(git + ["mktree", "-z"], capture_output=True, check=True, input=(
            b"\0".join(entries) + b"\0" + f"040000 tree {oth}\tother\0".encode()
            + f"040000 tree {res}\tresearch\0".encode())).stdout.strip().decode()
        new = subprocess.run(git + ["-c", "user.name=t", "-c", "user.email=t@example.com",
                                    "commit-tree", tree, "-p", old, "-m", "gitlinks"],
                             capture_output=True, check=True).stdout.strip().decode()
        cfg = tmp_path / "gitconfig"
        cfg.write_text("[diff]\n\tignoreSubmodules = all\n" if where == "global" else "",
                       encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        if where == "hub":
            subprocess.run(git + ["config", "submodule.sub.path", "other/sub"], check=True)
            subprocess.run(git + ["config", "submodule.sub.ignore", "all"], check=True)
        monkeypatch.chdir(repo)
        expected = ["other/sub", "research/mario/x.md"]
        assert sorted(get_changed_files(old, new)) == expected
        subprocess.run(["git", "update-ref", "-d", "refs/heads/main"], check=True)
        assert sorted(get_changed_files(ZERO_SHA, new)) == [".gitmodules"] + expected

    def test_a_listing_cut_short_refuses_instead_of_crashing(self):
        from schist.pre_receive import _raw_entries
        with pytest.raises(subprocess.CalledProcessError):
            list(_raw_entries([b"\n:100644 100644 " + b"a" * 40 + b" " + b"b" * 40 + b" M"]))
        with pytest.raises(subprocess.CalledProcessError):
            list(_raw_entries([b":100644 100644", b"x.md"]))

    def test_a_timeout_kills_the_whole_process_group(self):
        """A wrapper whose child keeps stdout open held the read until the
        child exited, well past the timeout."""
        import time
        from schist.pre_receive import _git_nul_tokens
        start = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            list(_git_nul_tokens(["sh", "-c", "sleep 30 & sleep 30"], 0.5))
        assert time.monotonic() - start < 10

    @pytest.mark.parametrize("data", [
        b"---js\n{}\n", b"---\ntitle: x\n", b"\xef\xbb\xbf---js\n", b"----\n", b"plain\n", b"",
        b"---" + b" " * 10_000 + b"js\nbody\n",       # first line longer than the head
        b"---" + b" " * 10_000 + b"\nbody js\n",      # long blank first line, text after it
        b"---" + b" " * 10_000,                        # no newline at all
        b"--- js" + b"\n" + b"x" * 200_000,
    ], ids=range(10))
    def test_the_streamed_opener_answers_like_the_whole_blob(self, data):
        import io
        from schist.pre_receive import _bad_opener, _read_opener
        assert _bad_opener(_read_opener(io.BytesIO(data), len(data))) == _bad_opener(data)

    def test_a_large_note_is_not_held_in_memory(self, tmp_path):
        """A 1 GB note compresses to a few MB, and reading it whole held
        gigabytes on the hub. Measured in a fresh interpreter, whose peak RSS
        reflects only the scan, with the blob written to disk in chunks."""
        repo, (old,) = _bare_repo_with(tmp_path, [SEED])
        note = tmp_path / "big.md"
        with open(note, "wb") as f:
            f.write(b"---\ntitle: big\n---\n")
            for _ in range(300):
                f.write(b"x" * (1 << 20))
        git = ["git", "-C", str(repo)]
        blob = subprocess.run(git + ["hash-object", "-w", str(note)], capture_output=True,
                              check=True).stdout.strip().decode()
        note.unlink()
        tree = subprocess.run(git + ["mktree"], input=f"100644 blob {blob}\tbig.md\n".encode(),
                              capture_output=True, check=True).stdout.strip().decode()
        new = subprocess.run(git + ["-c", "user.name=t", "-c", "user.email=t@example.com",
                                    "commit-tree", tree, "-p", old, "-m", "big"],
                             capture_output=True, check=True).stdout.strip().decode()
        code = ("import resource, sys\n"
                "from schist.pre_receive import find_bad_frontmatter, get_changed_files\n"
                "old, new = sys.argv[1:3]\n"
                "assert find_bad_frontmatter(old, new, get_changed_files(old, new)) == []\n"
                "print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)\n")
        env = {k: v for k, v in os.environ.items() if k != "GIT_DIR"}
        r = subprocess.run([sys.executable, "-c", code, old, new], cwd=repo, env=env,
                           capture_output=True, text=True, encoding="utf-8", check=True)
        peak_mb = _peak_rss_mb(int(r.stdout.strip()))
        assert peak_mb < 150, peak_mb

    def test_peak_rss_is_read_in_each_platforms_unit(self):
        """ru_maxrss is KiB on Linux and bytes on macOS; dividing by 1024
        everywhere read a Mac's 40 MB as 40,000 MB."""
        assert _peak_rss_mb(40 << 20, "darwin") == 40
        assert _peak_rss_mb(40 << 10, "linux") == 40

    def test_get_changed_files_keeps_a_path_that_begins_with_a_line_break(self, tmp_path,
                                                                          monkeypatch):
        """Nothing is stripped from a listed path: `\nresearch.md` is not
        `research.md`, and stripping a leading line break would let a root
        path such as `\nresearch/alpha/x.md` be checked as one inside a
        granted scope."""
        from schist.pre_receive import get_changed_files
        repo, (old,) = _bare_repo_with(tmp_path, [SEED])
        git = ["git", "-C", str(repo)]
        blob = subprocess.run(git + ["hash-object", "-w", "--stdin"], input=b"x\n",
                              capture_output=True, check=True).stdout.strip().decode()
        base = subprocess.run(git + ["ls-tree", "-z", old], capture_output=True,
                              check=True).stdout
        tree = subprocess.run(git + ["mktree", "-z"], capture_output=True, check=True,
                              input=base + f"100644 blob {blob}\t\nresearch.md\0".encode()
                              ).stdout.strip().decode()
        new = subprocess.run(git + ["-c", "user.name=t", "-c", "user.email=t@example.com",
                                    "commit-tree", tree, "-p", old, "-m", "nl"],
                             capture_output=True, check=True).stdout.strip().decode()
        monkeypatch.chdir(repo)
        assert get_changed_files(old, new) == ["\nresearch.md"]

    @pytest.mark.parametrize("listing", ["scope", "scan"])
    def test_path_bytes_are_capped(self, tmp_path, monkeypatch, listing):
        """Nested directories make each path long while the pushed trees stay
        small; the entry cap alone let about 2 MB pushed hold gigabytes."""
        import schist.pre_receive as pr
        repo, (old, new) = _bare_repo_with(tmp_path, [SEED, {
            f"research/mario/n{i}.md": b"n\n" for i in range(20)}])
        monkeypatch.chdir(repo)
        monkeypatch.setattr(pr, "SCAN_BYTE_LIMIT", 200)
        with pytest.raises(pr.ScanLimitExceeded, match="MB of paths"):
            if listing == "scope":
                pr.get_changed_files(old, new)
            else:
                pr._pushed_markdown_blobs(old, new)

    def test_a_path_longer_than_path_max_is_refused(self, tmp_path, monkeypatch, git_version):
        """The scope listing, which runs first, decides; the frontmatter scan
        only counts, so it does not refuse a long path the push deletes."""
        import schist.pre_receive as pr
        repo, (old, new) = _bare_repo_with(tmp_path, [SEED, {
            "research/mario/a-rather-long-note-name.md": b"n\n"}])
        monkeypatch.chdir(repo)
        monkeypatch.setattr(pr, "MAX_PATH_BYTES", 30)
        with pytest.raises(pr.ScanLimitExceeded, match="path longer than 30 bytes"):
            pr.get_changed_files(old, new)
        assert list(pr._pushed_markdown_blobs(old, new).values()) == [
            ["research/mario/a-rather-long-note-name.md"]]

    def test_a_long_path_holding_a_line_break_is_checked_against_the_tip(self, tmp_path,
                                                                        monkeypatch, git_version):
        """A name holding a line break cannot go to a line-based cat-file."""
        import schist.pre_receive as pr
        repo, (old,) = _bare_repo_with(tmp_path, [SEED])
        git = ["git", "-C", str(repo)]
        blob = subprocess.run(git + ["hash-object", "-w", "--stdin"], input=b"x\n",
                              capture_output=True, check=True).stdout.strip().decode()
        base = subprocess.run(git + ["ls-tree", "-z", old], capture_output=True,
                              check=True).stdout
        name = "a-long\nname-that-is-over-thirty-bytes.md"
        tree = subprocess.run(git + ["mktree", "-z"], capture_output=True, check=True,
                              input=base + f"100644 blob {blob}\t{name}\0".encode()
                              ).stdout.strip().decode()
        new = subprocess.run(git + ["-c", "user.name=t", "-c", "user.email=t@example.com",
                                    "commit-tree", tree, "-p", old, "-m", "nl"],
                             capture_output=True, check=True).stdout.strip().decode()
        monkeypatch.chdir(repo)
        monkeypatch.setattr(pr, "MAX_PATH_BYTES", 30)
        with pytest.raises(pr.ScanLimitExceeded, match="path longer than 30 bytes"):
            pr.get_changed_files(old, new)
        assert name in pr.get_changed_files(new, old)      # the push that deletes it

    @pytest.mark.parametrize("present", [[], ["plain"], ["newline"], ["plain", "newline"]])
    def test_long_names_with_and_without_line_breaks_are_looked_up(self, tmp_path, monkeypatch,
                                                                   git_version, present):
        """Each lookup path answers for its own names, together in one push."""
        import schist.pre_receive as pr
        names = {"plain": "a-rather-long-name-over-thirty bytes.md",
                 "newline": "another\nrather-long-name-over-thirty.md"}
        repo, (old,) = _bare_repo_with(tmp_path, [SEED])
        git = ["git", "-C", str(repo)]
        blob = subprocess.run(git + ["hash-object", "-w", "--stdin"], input=b"x\n",
                              capture_output=True, check=True).stdout.strip().decode()
        base = subprocess.run(git + ["ls-tree", "-z", old], capture_output=True,
                              check=True).stdout
        tree = subprocess.run(git + ["mktree", "-z"], capture_output=True, check=True,
                              input=base + b"".join(f"100644 blob {blob}\t{names[k]}\0".encode()
                                                    for k in present)).stdout.strip().decode()
        new = subprocess.run(git + ["-c", "user.name=t", "-c", "user.email=t@example.com",
                                    "commit-tree", tree, "-p", old, "-m", "t"],
                             capture_output=True, check=True).stdout.strip().decode()
        monkeypatch.chdir(repo)
        monkeypatch.setattr(pr, "MAX_PATH_BYTES", 30)
        asked = [n.encode() for n in names.values()]
        if present:
            with pytest.raises(pr.ScanLimitExceeded, match="path longer than 30 bytes"):
                pr._refuse_long_paths(new, asked)
        else:
            pr._refuse_long_paths(new, asked)

    @pytest.mark.parametrize("entry", [
        "100644 blob {blob}\ta-rather-long-name-over-thirty-bytes.md\r",
        "100644 blob {blob}\t\ra-rather-long-name-over-thirty-bytes.md",
        "100644 blob {blob}\ta-rather-long\r\nname-over-thirty-bytes.md\r",
        "160000 commit {commit}\ta-rather-long-gitlink-over-thirty-bytes",
    ])
    def test_a_long_name_in_the_tip_is_found_whatever_bytes_it_holds(
            self, tmp_path, monkeypatch, git_version, entry):
        """Looked up by listing the tip, not by name: a line-based cat-file
        drops a trailing carriage return, so a name ending in one was looked
        up as a different name, read as missing and let through; a gitlink
        names a commit the hub need not have, so cat-file read it as missing
        too."""
        import schist.pre_receive as pr
        repo, (old,) = _bare_repo_with(tmp_path, [SEED])
        git = ["git", "-C", str(repo)]
        blob = subprocess.run(git + ["hash-object", "-w", "--stdin"], input=b"x\n",
                              capture_output=True, check=True).stdout.strip().decode()
        base = subprocess.run(git + ["ls-tree", "-z", old], capture_output=True,
                              check=True).stdout
        line = entry.format(blob=blob, commit="1" * 40)
        tree = subprocess.run(git + ["mktree", "-z", "--missing"], capture_output=True,
                              check=True, input=base + line.encode() + b"\0"
                              ).stdout.strip().decode()
        new = subprocess.run(git + ["-c", "user.name=t", "-c", "user.email=t@example.com",
                                    "commit-tree", tree, "-p", old, "-m", "t"],
                             capture_output=True, check=True).stdout.strip().decode()
        monkeypatch.chdir(repo)
        monkeypatch.setattr(pr, "MAX_PATH_BYTES", 30)
        name = line.split("\t", 1)[1]
        with pytest.raises(pr.ScanLimitExceeded, match="path longer than 30 bytes"):
            pr._refuse_long_paths(new, [name.encode()])
        with pytest.raises(pr.ScanLimitExceeded, match="path longer than 30 bytes"):
            pr.get_changed_files(old, new)
        assert name in pr.get_changed_files(new, old)      # the push that deletes it

    def test_the_tip_listing_counts_against_the_push_wide_totals(self, tmp_path, monkeypatch):
        """Each ref line listing the tip against a fresh total let a push of
        many ref lines list one large tip once per line."""
        import schist.pre_receive as pr
        name = "research/mario/a-rather-long-note-name.md"
        repo, (old, new) = _bare_repo_with(tmp_path, [SEED, {
            f"research/mario/n{i}.md": b"n\n" for i in range(20)}])
        monkeypatch.chdir(repo)
        monkeypatch.setattr(pr, "MAX_PATH_BYTES", 30)
        tip_entries = len(subprocess.run(["git", "ls-tree", "-r", "-z", "--name-only", new],
                                         capture_output=True, check=True).stdout.split(b"\0")) - 1
        totals = [0, 0]
        pr._refuse_long_paths(new, [name.encode()], totals)
        assert totals[0] == tip_entries
        monkeypatch.setattr(pr, "SCAN_ENTRY_LIMIT", tip_entries + 5)
        with pytest.raises(pr.ScanLimitExceeded, match="file versions"):
            pr._refuse_long_paths(new, [name.encode()], totals)

    def test_many_long_names_cost_one_git_process(self, tmp_path, monkeypatch):
        """One process per name holding a line break let a push of small
        trees spawn tens of thousands of git processes on the hub."""
        import schist.pre_receive as pr
        repo, (old,) = _bare_repo_with(tmp_path, [SEED])
        monkeypatch.chdir(repo)
        monkeypatch.setattr(pr, "MAX_PATH_BYTES", 30)
        spawned = []
        real_popen = subprocess.Popen

        def counting_popen(*args, **kwargs):
            spawned.append(args[0] if args else kwargs.get("args"))
            return real_popen(*args, **kwargs)

        monkeypatch.setattr(pr.subprocess, "Popen", counting_popen)
        names = [f"missing\nname-number-{i:03d}-over-thirty-bytes.md".encode() for i in range(50)]
        pr._refuse_long_paths(old, names)
        assert len(spawned) == 1

    def test_a_push_may_delete_a_long_path(self, tmp_path, monkeypatch, git_version):
        """A path over the limit that landed before the hook refused them must
        still be removable; only a long path left in the pushed tip refuses."""
        import schist.pre_receive as pr
        name = "research/mario/a-rather-long-note-name.md"
        repo, (base, with_long, without) = _bare_repo_with(tmp_path, [SEED, {name: b"n\n"},
                                                                     {name: None}])
        monkeypatch.chdir(repo)
        monkeypatch.setattr(pr, "MAX_PATH_BYTES", 30)
        assert name in pr.get_changed_files(with_long, without)
        # Added and deleted inside one push: still listed for the scope check
        # of the commit that held it, but not refused, since the tip has none.
        assert name in pr.get_changed_files(base, without)
        with pytest.raises(pr.ScanLimitExceeded, match="path longer than 30 bytes"):
            pr.get_changed_files(base, with_long)

    def test_main_prints_the_cap_that_was_hit(self, tmp_path, monkeypatch, capsys, git_version):
        import schist.pre_receive as pr
        from schist.acl import parse_vault_data
        repo, (old, new) = _bare_repo_with(tmp_path, [SEED, {
            "research/mario/a-rather-long-note-name.md": b"n\n"}])
        monkeypatch.chdir(repo)
        monkeypatch.setattr(pr, "MAX_PATH_BYTES", 30)
        acl = parse_vault_data({
            "name": "v", "vault_version": 1, "scope_convention": "subdirectory",
            "participants": [{"name": "admin", "type": "agent"}],
            "access": {"admin": {"read": ["*"], "write": ["*"]}}})
        rc = pr.main(stdin=[f"{old} {new} refs/heads/main"], acl=acl, identity="admin",
                     log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        assert ("REJECTED: push contains a path longer than 30 bytes; shorten it."
                in capsys.readouterr().err)

    def test_undecodable_bytes_are_found_like_the_character_loop(self):
        from schist.pre_receive import _has_undecodable_bytes
        for value in ["", "plain.md", "caf\u00e9.md", "a\udc80b", "x\udcff", "\udc7f", "\udd00",
                      "\ud800", "n\u2028.md"]:
            assert _has_undecodable_bytes(value) == any(0xDC80 <= ord(c) <= 0xDCFF for c in value)

    def test_get_changed_files_is_capped(self, tmp_path, monkeypatch):
        import schist.pre_receive as pr
        commits = [SEED] + [{f"research/mario/n{i}.md": b"n\n"} for i in range(10)]
        repo, shas = _bare_repo_with(tmp_path, commits)
        monkeypatch.chdir(repo)
        monkeypatch.setattr(pr, "SCAN_ENTRY_LIMIT", 5)
        with pytest.raises(pr.ScanLimitExceeded):
            pr.get_changed_files(shas[0], shas[-1])

    def test_a_long_token_is_read_in_linear_time(self):
        """One path is one token, and git puts no limit on a tree entry's
        name. Joining the open token to each 64 KB chunk took about 10 s for
        48 MB on the hub (and 4x per doubling); read linearly it is well
        under a second."""
        import time
        from schist.pre_receive import _git_nul_tokens
        size = 48 << 20
        cmd = [sys.executable, "-c",
               f"import sys; sys.stdout.buffer.write(b'a' * {size} + b'\\0b')"]
        start = time.monotonic()
        tokens = list(_git_nul_tokens(cmd, 60))
        assert time.monotonic() - start < 3
        assert [len(t) for t in tokens] == [size, 1]

    def test_tokens_split_across_chunks_are_kept_whole(self):
        from schist.pre_receive import _git_nul_tokens
        expected = [b"a" * 70_000, b"", b"b", b"c" * 200_000, b"d" * 65_535, b"tail"]
        lengths = [len(t) for t in expected]
        cmd = [sys.executable, "-c",
               "import sys\n"
               f"parts = [c * n for c, n in zip([b'a', b'', b'b', b'c', b'd'], {lengths[:5]})]\n"
               "sys.stdout.buffer.write(b'\\0'.join(parts + [b'tail']))"]
        assert list(_git_nul_tokens(cmd, 30)) == expected

    def test_an_open_token_past_the_byte_cap_is_refused_before_its_end(self, monkeypatch):
        import schist.pre_receive as pr
        monkeypatch.setattr(pr, "SCAN_BYTE_LIMIT", 100_000)
        # The token never ends: refused as too large, not as a timeout.
        cmd = [sys.executable, "-c",
               "import sys\nwhile True: sys.stdout.buffer.write(b'a' * 65536)"]
        with pytest.raises(pr.ScanLimitExceeded, match="MB of paths"):
            list(pr._git_nul_tokens(cmd, 20))

    def test_a_stalled_git_listing_times_out(self):
        from schist.pre_receive import _git_nul_tokens
        import time
        start = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            list(_git_nul_tokens(["sh", "-c", "sleep 30"], 0.5))
        assert time.monotonic() - start < 10


class TestRefPolicy:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_DIR", str(tmp_path))   # not a repo: HEAD falls back to main

    def _run(self, acl, identity, line, tmp_path):
        with patch("schist.pre_receive.get_changed_files", return_value=["research/mario/n.md"]):
            return main(stdin=[line], acl=acl, identity=identity,
                        log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")

    @pytest.mark.parametrize("ref", ["refs/heads/feature", "refs/tags/main", "refs/tags/v1"])
    def test_a_scoped_writer_may_only_push_the_served_branch(self, acl, tmp_path, capsys, ref):
        assert self._run(acl, "cluster-mario", f"aaa bbb {ref}", tmp_path) == 1
        err = capsys.readouterr().err
        assert ref in err and "refs/heads/main" in err

    def test_a_scoped_writer_may_not_delete_a_ref(self, acl, tmp_path, capsys):
        zero = "0" * 40
        assert self._run(acl, "cluster-mario", f"abc {zero} refs/heads/old", tmp_path) == 1
        assert "delete" in capsys.readouterr().err

    def test_the_served_branch_is_still_accepted(self, acl, tmp_path):
        assert self._run(acl, "cluster-mario", "aaa bbb refs/heads/main", tmp_path) == 0

    def test_a_wildcard_writer_may_push_and_delete_other_refs(self, acl, tmp_path):
        zero = "0" * 40
        assert self._run(acl, "admin", "aaa bbb refs/tags/v1", tmp_path) == 0
        assert self._run(acl, "admin", f"abc {zero} refs/heads/old", tmp_path) == 0

    def test_the_served_branch_follows_the_hub_head(self, acl, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("GIT_DIR")
        repo, _ = _bare_repo_with(tmp_path, [SEED])
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/trunk"], cwd=repo, check=True)
        monkeypatch.chdir(repo)
        assert self._run(acl, "cluster-mario", "aaa bbb refs/heads/trunk", tmp_path) == 0
        assert self._run(acl, "cluster-mario", "aaa bbb refs/heads/main", tmp_path) == 1

    def test_a_hub_head_that_is_not_a_branch_refuses_scoped_writers(self, acl, tmp_path,
                                                                     monkeypatch, capsys):
        """Guessing refs/heads/main let a scoped writer create `main` on a
        hub serving another branch."""
        from schist.pre_receive import served_branch
        monkeypatch.delenv("GIT_DIR")
        repo, (sha,) = _bare_repo_with(tmp_path, [SEED])
        subprocess.run(["git", "update-ref", "--no-deref", "HEAD", sha], cwd=repo, check=True)
        monkeypatch.chdir(repo)
        assert served_branch() is None
        assert self._run(acl, "cluster-mario", "aaa bbb refs/heads/main", tmp_path) == 1
        assert "cannot read the branch it serves" in capsys.readouterr().err
        assert self._run(acl, "admin", "aaa bbb refs/heads/main", tmp_path) == 0

    def test_a_failed_head_read_raises(self, acl, tmp_path, monkeypatch):
        from schist.pre_receive import served_branch
        monkeypatch.delenv("GIT_DIR")
        repo, _ = _bare_repo_with(tmp_path, [SEED])
        monkeypatch.chdir(repo)
        real_run = subprocess.run

        def slow_head(args, *a, **k):
            if list(args[:2]) == ["git", "symbolic-ref"]:
                raise subprocess.TimeoutExpired(args, 10)
            return real_run(args, *a, **k)

        monkeypatch.setattr(subprocess, "run", slow_head)
        with pytest.raises(subprocess.TimeoutExpired):
            served_branch()

    def test_a_hub_that_cannot_read_its_branch_asks_for_a_retry(self, acl, tmp_path, monkeypatch,
                                                               capsys):
        """A timeout is the hub's fault and may clear: an ERROR line, which
        spokes read as transient, not a REJECTED refusal of the push."""
        def slow():
            raise subprocess.TimeoutExpired(["git", "symbolic-ref"], 10)
        monkeypatch.setattr("schist.pre_receive.served_branch", slow)
        assert self._run(acl, "cluster-mario", "aaa bbb refs/heads/main", tmp_path) == 1
        err = capsys.readouterr().err
        assert err.startswith("ERROR: the hub could not read the branch it serves (git timed out)")
        assert "REJECTED" not in err

    # Git allows these in a ref name; Python's no-argument split() and strip()
    # treat them as whitespace (NO-BREAK SPACE, NEXT LINE, LINE SEPARATOR,
    # IDEOGRAPHIC SPACE).
    @pytest.mark.parametrize("sep", [0x00A0, 0x0085, 0x2028, 0x3000], ids=hex)
    @pytest.mark.parametrize("tail", ["x", ""], ids=["inside", "trailing"])
    def test_unicode_whitespace_does_not_shorten_a_ref_name(self, acl, tmp_path, capsys,
                                                            sep, tail):
        ref = "refs/heads/main" + chr(sep) + tail
        assert self._run(acl, "cluster-mario", f"aaa bbb {ref}", tmp_path) == 1
        assert "may only push refs/heads/main" in capsys.readouterr().err

    @pytest.mark.parametrize("locale_encoding, pushed, shown", [
        # A trailing NO-BREAK SPACE on the last line survives reading stdin.
        ("utf-8", b"aaa bbb refs/heads/main\xc2\xa0\n", "refs/heads/main\xa0:"),
        # A name that is not UTF-8 reads the same under any locale sshd forwards.
        ("latin-1", b"aaa bbb refs/heads/main\xff\n", "refs/heads/main\\xff:"),
    ], ids=["trailing-nbsp", "not-utf8"])
    def test_ref_names_read_from_stdin_are_kept_whole(self, acl, tmp_path, monkeypatch, capsys,
                                                      locale_encoding, pushed, shown):
        import io
        import sys
        monkeypatch.setattr(sys, "stdin",
                            io.TextIOWrapper(io.BytesIO(pushed), encoding=locale_encoding))
        with patch("schist.pre_receive.get_changed_files", return_value=["research/mario/n.md"]):
            rc = main(acl=acl, identity="cluster-mario",
                      log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        assert shown in capsys.readouterr().err

    @pytest.mark.parametrize("identity", ["cluster-mario", "admin"])
    def test_a_line_the_hook_cannot_read_is_refused(self, acl, tmp_path, capsys, identity):
        assert self._run(acl, identity, "aaa bbb", tmp_path) == 1
        assert "not a ref update the hook can read" in capsys.readouterr().err

    def test_empty_lines_are_skipped(self, acl, tmp_path):
        with patch("schist.pre_receive.get_changed_files", return_value=["research/mario/n.md"]):
            assert main(stdin=["", "aaa bbb refs/heads/main", ""], acl=acl,
                        identity="cluster-mario", log_path=tmp_path / "rejected.log",
                        db_path=tmp_path / "rl.sqlite") == 0

    def test_a_non_ascii_served_branch_is_read_as_utf8_under_any_locale(self, tmp_path,
                                                                        monkeypatch):
        """served_branch() decodes HEAD the way main() decodes pushed ref
        names. Read by an ASCII or latin-1 locale, a non-ASCII branch name
        would match a differently named ref pushed alongside it."""
        monkeypatch.delenv("GIT_DIR")
        repo, _ = _bare_repo_with(tmp_path, [SEED])
        head = "refs/heads/ma" + chr(0xEF) + "n"
        subprocess.run(["git", "symbolic-ref", "HEAD", head], cwd=repo, check=True)
        env = {"PATH": os.environ["PATH"], "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0",
               "PYTHONUTF8": "0", "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        out = subprocess.run(
            [sys.executable, "-c",
             "from schist.pre_receive import served_branch; print(ascii(served_branch()))"],
            cwd=repo, env=env, capture_output=True, text=True)
        assert out.stdout.strip() == ascii(head), out.stderr


# ── Rejection message follow-ups to #781 ─────────────────────────────────────

class TestRejectionMessages:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_DIR", str(tmp_path))   # not a repo: HEAD falls back to main

    def test_acl_violations_are_reported_alongside_a_ref_refusal(self, acl, tmp_path, capsys):
        """#790: a push that broke the ref rule returned before the ACL
        violations found on the ref were printed or logged, so the pusher
        fixed one problem only to be refused again for the other."""
        with patch("schist.pre_receive.get_changed_files",
                   side_effect=lambda old, new, **k: ["security/out.md"]):
            rc = main(stdin=["aaa bbb refs/heads/feature"],
                      acl=acl, identity="cluster-mario",
                      log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        err = capsys.readouterr().err
        assert "refs/heads/feature" in err and "security/out.md" in err
        assert "security/out.md" in (tmp_path / "rejected.log").read_text(encoding="utf-8")

    def test_frontmatter_hint_names_the_served_branch(self):
        """#791: the reset hint said origin/main on a hub that serves another
        branch."""
        v = [Violation("x", "research/mario/x.md", FRONTMATTER_SCOPE, "refs/heads/trunk")]
        msg = format_rejection(v, branch="refs/heads/trunk")
        assert "git reset --soft origin/trunk" in msg and "origin/main" not in msg

    def test_frontmatter_hint_names_the_ref_that_carried_the_note(self):
        """A '*' writer pushing another branch is told to reset to that
        branch, not the served one, which would squash its history."""
        v = [Violation("x", "research/mario/x.md", FRONTMATTER_SCOPE, "refs/heads/feature")]
        assert "git reset --soft origin/feature" in format_rejection(v, branch="refs/heads/trunk")
        tag = [Violation("x", "research/mario/x.md", FRONTMATTER_SCOPE, "refs/tags/v1")]
        assert "git reset --soft origin/trunk" in format_rejection(tag, branch="refs/heads/trunk")

    def test_files_on_a_refused_ref_are_reported_too(self, acl, tmp_path, capsys):
        """#790's usual form: one ref the identity may not push, carrying a
        file it may not write. Both are reported, so moving the commits to
        the served branch is not refused a second time."""
        with patch("schist.pre_receive.get_changed_files", return_value=["security/out.md"]):
            rc = main(stdin=["aaa bbb refs/heads/feature"], acl=acl, identity="cluster-mario",
                      log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        err = capsys.readouterr().err
        assert "refs/heads/feature" in err and "security/out.md" in err

    @pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("git log", 30),
                                         subprocess.CalledProcessError(128, ["git", "log"])],
                             ids=["timeout", "git-error"])
    def test_a_ref_refusal_is_not_followed_by_a_retry_hint(self, acl, tmp_path, capsys, failure):
        """The push is refused for the ref whatever a retry finds; an ERROR
        line after it made both spoke-side classifiers read it as transient,
        so clients retried a push that can never land."""
        from schist.sync import classify_push_failure
        with patch("schist.pre_receive.get_changed_files", side_effect=failure):
            rc = main(stdin=["aaa bbb refs/heads/feature"],
                      acl=acl, identity="cluster-mario",
                      log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        err = capsys.readouterr().err
        assert err.startswith("REJECTED: push updates refs") and "refs/heads/feature" in err
        assert "ERROR" not in err and "retry" not in err
        relayed = "".join(f"remote: {line}\n" for line in err.splitlines())
        assert classify_push_failure(relayed) != "hub-transient"
        # Without a ref refusal the same failure still asks for a retry.
        with patch("schist.pre_receive.get_changed_files", side_effect=failure):
            assert main(stdin=["aaa bbb refs/heads/main"], acl=acl, identity="cluster-mario",
                        log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite") == 1
        assert capsys.readouterr().err.startswith("ERROR: ")

    def test_main_passes_the_served_branch_to_the_hint(self, acl, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("schist.pre_receive.served_branch", lambda: "refs/heads/trunk")
        monkeypatch.setattr("schist.pre_receive.find_bad_frontmatter",
                            lambda *a, **k: ["research/mario/x.md"])
        with patch("schist.pre_receive.get_changed_files", return_value=["research/mario/x.md"]):
            rc = main(stdin=["aaa bbb refs/heads/trunk"], acl=acl, identity="cluster-mario",
                      log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        assert "origin/trunk" in capsys.readouterr().err

    def test_header_does_not_claim_out_of_scope_for_content_only_refusals(self):
        """#795: a frontmatter-only (or bad-path-only) refusal is not a scope
        problem, so the header must not send the operator to vault.yaml. It
        keeps the `REJECTED:` prefix both spoke-side classifiers match."""
        from schist.pre_receive import NON_UTF8_SCOPE

        for scope in (FRONTMATTER_SCOPE, NON_UTF8_SCOPE):
            msg = format_rejection([Violation("x", "research/mario/x.md", scope, "refs/heads/main")])
            header = msg.splitlines()[0]
            assert header.startswith("REJECTED:") and "out-of-scope" not in header, scope
        mixed = format_rejection([
            Violation("x", "research/mario/x.md", FRONTMATTER_SCOPE, "refs/heads/main"),
            Violation("x", "security/x.md", "security", "refs/heads/main"),
        ])
        assert mixed.splitlines()[0] == "REJECTED: push contains out-of-scope writes"


WILDCARD_ONLY = {
    "name": "v", "vault_version": 1, "scope_convention": "subdirectory",
    "participants": [{"name": "admin", "type": "agent"}, {"name": "alpha", "type": "agent"}],
    "access": {"admin": {"read": ["*"], "write": ["*"]},
               "alpha": {"read": ["*"], "write": ["research/alpha"]}}}


class TestPushWideLimits:
    """The scan caps bound the whole push: each extra ref line naming the
    same commits listed their history again under a fresh count, and a
    refused ref was scanned too, so K ref lines cost K times the cap."""

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        monkeypatch.delenv("GIT_DIR", raising=False)
        monkeypatch.setattr("schist.pre_receive.served_branch", lambda: "refs/heads/main")

    def _repo(self, tmp_path, monkeypatch):
        repo, (old, new) = _bare_repo_with(tmp_path, [SEED, {
            f"research/alpha/n{i}.md": b"n\n" for i in range(4)}])
        monkeypatch.chdir(repo)
        return old, new

    @pytest.mark.reads_blobs
    @pytest.mark.parametrize("listing", ["scope", "scan"])
    def test_the_caps_count_every_ref_line_of_a_push(self, tmp_path, monkeypatch, capsys,
                                                     listing):
        """Each ref lists 8 entries (4 per commit, 4 in the net diff), under
        a cap of 20; four ref lines list 32."""
        import schist.pre_receive as pr
        old, new = self._repo(tmp_path, monkeypatch)
        monkeypatch.setattr(pr, "SCAN_ENTRY_LIMIT", 20)
        # Only the listing under test counts, so each total is pinned alone.
        other = "get_changed_files" if listing == "scan" else "find_bad_frontmatter"
        monkeypatch.setattr(pr, other, lambda *a, **k: [])
        acl = parse_vault_data(WILDCARD_ONLY)
        one = pr.main(stdin=[f"{old} {new} refs/heads/main"], acl=acl, identity="admin",
                      log_path=tmp_path / "r.log", db_path=tmp_path / "rl.sqlite")
        assert one == 0
        capsys.readouterr()
        rc = pr.main(stdin=[f"{old} {new} refs/heads/x{i}" for i in range(4)], acl=acl,
                     identity="admin", log_path=tmp_path / "r.log",
                     db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        assert "REJECTED: push is too large for the hub to check" in capsys.readouterr().err

    def test_a_shared_total_carries_across_calls(self, tmp_path, monkeypatch):
        import schist.pre_receive as pr
        old, new = self._repo(tmp_path, monkeypatch)
        monkeypatch.setattr(pr, "SCAN_ENTRY_LIMIT", 12)
        for listing in (pr.get_changed_files, pr._pushed_markdown_blobs):
            listing(old, new)
            listing(old, new)                   # a fresh count each call by default
            totals = [0, 0]
            listing(old, new, totals)
            assert totals[0] == 8
            with pytest.raises(pr.ScanLimitExceeded):
                listing(old, new, totals)

    def test_a_scoped_writers_multi_ref_push_is_refused_before_any_scan(self, tmp_path,
                                                                          capsys):
        """Git sends each ref once and such an identity may update only the
        served branch, so the push holds a refused ref whatever it carries;
        every refusal is named, the served branch's own included."""
        acl = parse_vault_data(WILDCARD_ONLY)
        log = tmp_path / "r.log"

        def no_scan(*a, **k):
            raise AssertionError("scanned")
        with patch("schist.pre_receive.get_changed_files", side_effect=no_scan), \
                patch("schist.pre_receive.find_bad_frontmatter", side_effect=no_scan):
            rc = main(stdin=["aaa bbb refs/heads/main"] +
                      [f"aaa bbb refs/heads/x{i}" for i in range(3)],
                      acl=acl, identity="alpha", log_path=log, db_path=tmp_path / "rl.sqlite")
            assert rc == 1
            err = capsys.readouterr().err
            assert [f"refs/heads/x{i}" in err for i in range(3)] == [True] * 3
            assert "refs/heads/main:" not in err
            rc = main(stdin=["aaa bbb refs/heads/x0", f"aaa {'0' * 40} refs/heads/main"],
                      acl=acl, identity="alpha", log_path=log, db_path=tmp_path / "rl.sqlite")
            assert rc == 1
            assert "refs/heads/main: only a '*' writer may delete a ref" in capsys.readouterr().err
            # Not something git sends, but two lines that each pass alone
            # are refused too.
            rc = main(stdin=["aaa bbb refs/heads/main", "ccc ddd refs/heads/main"],
                      acl=acl, identity="alpha", log_path=log, db_path=tmp_path / "rl.sqlite")
            assert rc == 1
            assert "may update only the served branch" in capsys.readouterr().err
        assert log.read_text(encoding="utf-8").count("REF_REJECTED identity=alpha") == 6

    def test_a_refusal_prints_and_logs_a_bounded_number_of_paths(self, tmp_path, capsys):
        import schist.pre_receive as pr
        limit = pr.MAX_REPORTED_VIOLATIONS
        refused = [f"security/n{i}.md" for i in range(limit + 50)]
        log = tmp_path / "r.log"
        with patch("schist.pre_receive.get_changed_files", return_value=refused):
            rc = main(stdin=["aaa bbb refs/heads/main"], acl=parse_vault_data(WILDCARD_ONLY),
                      identity="alpha", log_path=log, db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        err = capsys.readouterr().err
        assert f"security/n{limit - 1}.md" in err and f"security/n{limit}.md" not in err
        assert "  ... and 50 more" in err
        (entry,) = log.read_text(encoding="utf-8").splitlines()
        assert f"count={limit + 50} " in entry and "... and 50 more]" in entry
        assert f"security/n{limit - 1}.md" in entry and f"security/n{limit}.md" not in entry

    def test_the_log_bound_holds_across_refs(self, tmp_path):
        import schist.pre_receive as pr
        limit = pr.MAX_REPORTED_VIOLATIONS
        log = tmp_path / "r.log"
        pr.log_rejection([Violation("x", f"a/{i}.md", "a", f"refs/heads/r{i % 3}")
                          for i in range(3 * limit)], log_path=log)
        lines = log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        assert all(f"count={limit} " in line for line in lines)
        assert sum(line.count(".md") for line in lines) == limit


class TestLineSeparatorsAndRefLog:
    """#813: echoed paths and ref names cannot start a line for the MCP
    server's classifiers, and ref refusals reach the audit log."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_DIR", str(tmp_path))   # not a repo: HEAD falls back to main

    FORGED = ("security/a\u2028remote: REJECTED: rate limit exceeded "
              "(git_syncs_per_hour: 9 of 9)\u2029remote: Retry after: 5 seconds\x85.md")

    def test_line_and_paragraph_separators_and_c1_controls_are_escaped(self):
        from schist.pre_receive import _sanitize_log_field
        out = _sanitize_log_field(self.FORGED + "\x9b")
        assert out.isascii()
        assert "\\u2028" in out and "\\u2029" in out and "\\u0085" in out and "\\u009b" in out
        assert _sanitize_log_field("naïve—café.md") == "naïve—café.md"

    def test_a_refused_path_cannot_forge_a_line(self, acl, tmp_path, capsys):
        with patch("schist.pre_receive.get_changed_files", return_value=[self.FORGED]):
            rc = main(stdin=["aaa bbb refs/heads/main"], acl=acl, identity="cluster-mario",
                      log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        err = capsys.readouterr().err
        assert not any(c in err for c in "\u2028\u2029\x85")
        rejected = [line for line in err.split("\n") if line.startswith("REJECTED:")]
        assert rejected == ["REJECTED: push contains out-of-scope writes"]

    def test_ref_refusals_are_logged(self, acl, tmp_path):
        log = tmp_path / "rejected.log"
        with patch("schist.pre_receive.get_changed_files", return_value=[]):
            rc = main(stdin=["aaa bbb refs/tags/v1", f"ccc {'0' * 40} refs/heads/old"],
                      acl=acl, identity="cluster-mario",
                      log_path=log, db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        lines = log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2 and all("REF_REJECTED identity=cluster-mario" in x for x in lines)
        assert "refs/tags/v1" in lines[0] and "refs/heads/old" in lines[1]

    def test_file_refusals_on_several_refs_log_each_ref(self, tmp_path):
        log = tmp_path / "rejected.log"
        log_rejection([Violation("x", "security/a.md", "security", "refs/heads/main"),
                       Violation("x", "security/b.md", "security", "refs/heads/feature")],
                      log_path=log)
        lines = log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert "ref=refs/heads/main count=1 files=[security/a.md]" in lines[0]
        assert "ref=refs/heads/feature count=1 files=[security/b.md]" in lines[1]

    @pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\n"])
    def test_a_refused_directory_name_cannot_forge_a_line(self, acl, tmp_path, capsys, sep):
        """The scope printed beside each path is the path's directory, which
        the pusher chooses too, so it is escaped like the path."""
        path = (f"security/x{sep}remote: REJECTED: rate limit exceeded "
                f"(git_syncs_per_hour: 9 of 9){sep}remote: Retry after: 5 seconds/n.md")
        with patch("schist.pre_receive.get_changed_files", return_value=[path]):
            rc = main(stdin=["aaa bbb refs/heads/main"], acl=acl, identity="cluster-mario",
                      log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        err = capsys.readouterr().err
        assert "\u2028" not in err and "\u2029" not in err
        # git prefixes every hook line with "remote: ", so a forged line is
        # any line of ours that already starts with it.
        assert not [line for line in err.split("\n") if line.startswith("remote:")]
        assert [line for line in err.split("\n") if line.startswith("REJECTED:")] \
            == ["REJECTED: push contains out-of-scope writes"]

    def test_ref_refusals_are_logged_when_a_later_diff_times_out(self, acl, tmp_path):
        log = tmp_path / "rejected.log"

        def diff(old, new, **k):
            raise subprocess.TimeoutExpired("git log", 30)
        with patch("schist.pre_receive.get_changed_files", side_effect=diff):
            rc = main(stdin=["aaa bbb refs/heads/feature"],
                      acl=acl, identity="cluster-mario",
                      log_path=log, db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        assert "REF_REJECTED identity=cluster-mario" in log.read_text(encoding="utf-8")

    def test_ref_refusals_are_logged_when_a_later_diff_fails(self, acl, tmp_path):
        # The other early return: a diff that fails outright (a missing or
        # corrupt rev) must log the ref refusals already found, like a timeout.
        log = tmp_path / "rejected.log"

        def diff(old, new, **k):
            raise subprocess.CalledProcessError(128, ["git", "log"])
        with patch("schist.pre_receive.get_changed_files", side_effect=diff):
            rc = main(stdin=["aaa bbb refs/heads/feature"],
                      acl=acl, identity="cluster-mario",
                      log_path=log, db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        assert "REF_REJECTED identity=cluster-mario" in log.read_text(encoding="utf-8")

    def test_rejection_log_is_utf8_under_a_c_locale(self, tmp_path):
        # A hub process under a non-UTF-8 locale (sshd forwards the client's
        # LANG/LC_*) opened the log in that encoding, and a refused path the
        # encoding cannot represent raised UnicodeEncodeError, which the OSError
        # handler does not catch, so the hook crashed instead of refusing
        # cleanly. C with locale coercion and UTF-8 mode off is the portable
        # stand-in. The child's source stays ASCII (the path is built with
        # chr()), because a C-locale interpreter cannot decode a non-ASCII -c
        # argument.
        log = tmp_path / "rejected.log"
        code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from schist.pre_receive import Violation, log_rejection\n"
            "path = 'security/na' + chr(0xef) + 've' + chr(0x2014) + 'caf' + chr(0xe9) + '.md'\n"
            "log_rejection([Violation(identity='cluster-mario', filepath=path, scope='security',\n"
            "                          refname='refs/heads/main')],\n"
            "              log_path=Path(sys.argv[1]))\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("LC_")}
        env.update(LC_ALL="C", LANG="C", PYTHONCOERCECLOCALE="0", PYTHONUTF8="0")
        result = subprocess.run([sys.executable, "-c", code, str(log)],
                                env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        assert "security/naïve—café.md" in log.read_bytes().decode("utf-8")

    def test_spoke_side_display_path_escapes_line_separators(self):
        from schist.git_ops import _display_path
        assert _display_path("a\u2028b\u2029c.md") == "a\\u2028b\\u2029c.md"
        assert _display_path("naïve.md") == "naïve.md"


# ── The hook reads the same names under any locale ──────────────────────────

def _installed_locales() -> set[str]:
    try:
        out = subprocess.run(["locale", "-a"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.strip() for line in out.splitlines()}


# sshd forwards the client's LANG/LC_*, so the hook can run under any locale
# the hub has installed. An ASCII locale (C, with coercion to UTF-8 off) needs
# nothing installed; latin-9 is where a name decoded by the locale reads as
# a different, valid string, which is the bypass itself.
_LATIN9 = next((name for name in ("en_US.iso885915", "en_GB.iso885915")
                if name in _installed_locales()), None)
HOOK_LOCALES = [
    pytest.param("C", id="ascii"),
    pytest.param(_LATIN9 or "en_US.iso885915", id="latin9",
                 marks=pytest.mark.skipif(_LATIN9 is None, reason="no latin-9 locale installed")),
]


def _run_in_locale(locale_name: str, cwd: Path, code: str, *args: str) -> subprocess.CompletedProcess:
    """Run `code` (ASCII source) in a fresh interpreter under `locale_name`."""
    env = {"PATH": os.environ["PATH"], "LC_ALL": locale_name, "LANG": locale_name,
           "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    return subprocess.run([sys.executable, "-c", code, *args], cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


@pytest.mark.parametrize("locale_name", HOOK_LOCALES)
def test_a_non_ascii_served_branch_resolves_under_any_locale(tmp_path, monkeypatch,
                                                             locale_name):
    """HEAD's target goes back to `git rev-parse` as the bytes git printed.
    Passed as a str, it was encoded by the locale: under latin-9 the branch
    `refs/heads/ma\u00efn` went back as a name that does not exist and every
    push was refused, and under an ASCII locale it could not be encoded."""
    monkeypatch.delenv("GIT_DIR", raising=False)
    vault = ("name: v\nvault_version: 1\nscope_convention: subdirectory\n"
             "participants:\n  - {name: alpha, type: agent}\n"
             "access:\n  alpha: {read: ['*'], write: ['research/alpha']}\n").encode()
    repo, (tip,) = _bare_repo_with(tmp_path, [{"vault.yaml": vault}])
    branch = "refs/heads/ma\u00efn".encode("utf-8")
    subprocess.run([b"git", b"update-ref", branch, tip.encode()], cwd=repo, check=True)
    subprocess.run([b"git", b"symbolic-ref", b"HEAD", branch], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "-d", "refs/heads/main"], cwd=repo, check=True)
    code = ("import sys\n"
            "from schist.pre_receive import extract_vault_yaml_from_git\n"
            "from schist.doctor import check_hub_vault_yaml\n"
            "print(ascii(extract_vault_yaml_from_git()))\n"
            "r = check_hub_vault_yaml(sys.argv[1])\n"
            "print(r.status, ascii(r.message))\n")
    r = _run_in_locale(locale_name, repo, code, str(repo))
    assert r.returncode == 0, r.stderr
    content, doctor = r.stdout.splitlines()
    assert content == ascii(vault.decode()), r.stdout
    assert doctor.startswith("PASS"), doctor


def test_hook_modules_name_an_encoding_for_every_text_read_and_write():
    """The rule behind the locale tests below, for code they do not reach:
    every text-mode subprocess call and file open in the modules the hook
    imports names a fixed encoding, the hook reads its standard streams as
    bytes, and nothing decodes by the locale. Each module must have at least
    one call the scan checked, so a rename that hides a module's calls from
    it fails here instead of passing with nothing checked."""
    import ast
    root = Path(__file__).resolve().parents[1] / "schist"
    checked, bad = {}, []
    # Calls that pick the locale's encoding, or open text streams this scan
    # cannot check: none belongs in the hook's modules.
    locale_calls = {"getpreferredencoding", "getfilesystemencoding", "fsdecode", "fsencode",
                    "TextIOWrapper", "fdopen", "NamedTemporaryFile", "TemporaryFile",
                    "SpooledTemporaryFile", "popen"}

    def fixed_encoding(kw):
        enc = kw.get("encoding")
        return isinstance(enc, ast.Constant) and isinstance(enc.value, str)

    for name in ("pre_receive.py", "acl.py", "rate_limit.py"):
        checked[name] = 0
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            # sys.stdin / sys.stdout decode and encode by the locale; the hook
            # may use them only as sys.stdin.buffer (the original defect).
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "sys" and node.attr in ("stdin", "stdout")):
                up = parents.get(node)
                if not (isinstance(up, ast.Attribute) and up.attr == "buffer"):
                    bad.append(f"{name}:{node.lineno} sys.{node.attr} without .buffer")
            if not isinstance(node, ast.Call):
                continue
            func = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if func in locale_calls:
                bad.append(f"{name}:{node.lineno} {func}() decodes by the locale or opens an unchecked text stream")
                continue
            if any(k.arg is None for k in node.keywords) and func in (
                    "run", "check_output", "check_call", "call", "Popen", "open"):
                bad.append(f"{name}:{node.lineno} {func}(**kwargs) cannot be checked")
                continue
            kw = {k.arg: k.value for k in node.keywords}
            if func == "decode" and node.args and not (
                    isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                bad.append(f"{name}:{node.lineno} decode() with a computed encoding")
            if func in ("run", "check_output", "check_call", "call", "Popen"):
                flags = [kw[k] for k in ("text", "universal_newlines") if k in kw]
                if any(not isinstance(f, ast.Constant) for f in flags):
                    bad.append(f"{name}:{node.lineno} {func}() with a non-constant text flag")
                    continue
                # encoding= or errors= alone also switches to text mode.
                if any(f.value for f in flags) or "encoding" in kw or "errors" in kw:
                    checked[name] += 1
                    if not fixed_encoding(kw):
                        bad.append(f"{name}:{node.lineno} {func}() in text mode without a fixed encoding")
            elif func in ("open", "read_text", "write_text"):
                # builtin open(path, mode); Path(...).open(mode)
                pos = 1 if isinstance(node.func, ast.Name) else 0
                mode = node.args[pos] if len(node.args) > pos else kw.get("mode")
                if func == "open" and isinstance(mode, ast.Constant) and "b" in str(mode.value):
                    continue
                checked[name] += 1
                if not fixed_encoding(kw):
                    bad.append(f"{name}:{node.lineno} {func}() without a fixed encoding")
    assert all(checked.values()), checked   # the scan reached each module's calls
    assert bad == []


# ── A vault.yaml the hook cannot read refuses the push ─────────────────────

_ALPHA_VAULT = ("name: v\nvault_version: 1\nscope_convention: subdirectory\n"
                "participants:\n  - {name: alpha, type: agent}\n"
                "access:\n  alpha: {read: ['*'], write: ['research/alpha']}\n").encode("utf-8")


def _loosen(repo):
    """Unpack the repo's packs into loose objects, so one can be removed."""
    for pack in (repo / "objects" / "pack").glob("*.pack"):
        data = pack.read_bytes()
        for f in pack.parent.glob(pack.stem + ".*"):
            f.unlink()
        subprocess.run(["git", "unpack-objects", "-q"], cwd=repo, input=data, check=True)


def _drop_object(repo, rev):
    sha = subprocess.run(["git", "rev-parse", rev], cwd=repo, capture_output=True,
                         text=True, encoding="utf-8", check=True).stdout.strip()
    (repo / "objects" / sha[:2] / sha[2:]).unlink()


ZERO_SHA_T = "0" * 40


class TestVaultYamlAbsence:
    """Only a HEAD without vault.yaml lets a push through unchecked; a
    vault.yaml the hook cannot read refuses it."""

    @pytest.fixture(autouse=True)
    def _no_git_dir(self, monkeypatch):
        monkeypatch.delenv("GIT_DIR", raising=False)
        monkeypatch.delenv("GIT_WORK_TREE", raising=False)

    def test_an_unborn_head_has_no_vault_yaml(self, tmp_path, monkeypatch):
        from schist.pre_receive import extract_vault_yaml_from_git
        repo = tmp_path / "hub.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(repo)], check=True)
        monkeypatch.chdir(repo)
        assert extract_vault_yaml_from_git() is None

    @pytest.mark.parametrize("store", ["pack", "loose", "alternates"])
    def test_a_hub_with_objects_but_no_refs_raises(self, tmp_path, monkeypatch, store):
        """Refs lost on a hub that holds commits (a crash after pack-refs, a
        restore of objects without refs) is not a fresh hub: `schist hub
        init` always seeds a commit holding vault.yaml, and reading the state
        as "no vault.yaml" let the first pusher create main with any ACL."""
        from schist.pre_receive import extract_vault_yaml_from_git
        repo, _ = _bare_repo_with(tmp_path, [{"vault.yaml": _ALPHA_VAULT}])
        if store == "loose":
            _loosen(repo)
        if store == "alternates":
            other = tmp_path / "empty.git"
            subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
            (other / "objects" / "info" / "alternates").write_text(
                str(repo / "objects") + "\n", encoding="utf-8")
            repo = other
        else:
            subprocess.run(["git", "update-ref", "-d", "refs/heads/main"], cwd=repo, check=True)
        assert subprocess.run(["git", "for-each-ref"], cwd=repo, capture_output=True,
                              check=True).stdout == b""
        monkeypatch.chdir(repo)
        with pytest.raises(RuntimeError, match="the hub has objects but no refs"):
            extract_vault_yaml_from_git()
        monkeypatch.chdir(tmp_path)
        with pytest.raises(RuntimeError, match="git fsck --lost-found"):
            extract_vault_yaml_from_git(git_dir=str(repo))

    def test_main_refuses_a_push_to_a_hub_that_lost_its_refs(self, tmp_path, monkeypatch, capsys):
        from schist.pre_receive import main
        repo, (old, new) = _bare_repo_with(tmp_path, [{"vault.yaml": _ALPHA_VAULT},
                                                      {"outside.md": b"o\n"}])
        subprocess.run(["git", "update-ref", "-d", "refs/heads/main"], cwd=repo, check=True)
        monkeypatch.chdir(repo)
        rc = main(stdin=[f"{ZERO_SHA_T} {new} refs/heads/main"], identity="alpha",
                  log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        assert capsys.readouterr().err.startswith(
            "REJECTED: failed to load vault.yaml: the hub has objects but no refs")

    def test_the_first_push_to_an_empty_hub_is_accepted(self, tmp_path, monkeypatch):
        """The pushed objects sit in the quarantine directory under objects/
        while pre-receive runs; they are not the hub's own."""
        hub = tmp_path / "hub.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(hub)], check=True)
        hook = hub / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            f"exec '{sys.executable}' -c 'import os, sys\n"
            "assert os.environ[\"GIT_QUARANTINE_PATH\"]\n"
            "assert any(os.scandir(os.environ[\"GIT_QUARANTINE_PATH\"]))\n"
            "from schist.pre_receive import extract_vault_yaml_from_git as e\n"
            "sys.exit(0 if e() is None else 3)'\n", encoding="utf-8")
        hook.chmod(0o755)
        work = tmp_path / "work"
        git = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        (work / "vault.yaml").write_bytes(_ALPHA_VAULT)
        subprocess.run(git + ["add", "vault.yaml"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "v"], check=True)
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        r = subprocess.run(git + ["push", "-q", str(hub), "main"], env=env,
                           capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr
        # And once the hub holds that commit, losing the ref refuses.
        subprocess.run(["git", "update-ref", "-d", "refs/heads/main"], cwd=hub, check=True)
        r = subprocess.run(git + ["push", "-q", str(hub), "main"], env=env,
                           capture_output=True, text=True, encoding="utf-8")
        assert r.returncode != 0 and "objects but no refs" in r.stderr

    def test_a_head_tree_without_vault_yaml_has_none(self, tmp_path, monkeypatch):
        from schist.pre_receive import extract_vault_yaml_from_git
        repo, _ = _bare_repo_with(tmp_path, [{"README.md": b"r\n"}])
        monkeypatch.chdir(repo)
        assert extract_vault_yaml_from_git() is None

    def test_vault_yaml_is_read_from_head(self, tmp_path, monkeypatch):
        from schist.pre_receive import extract_vault_yaml_from_git
        repo, _ = _bare_repo_with(tmp_path, [{"vault.yaml": _ALPHA_VAULT}])
        monkeypatch.chdir(repo)
        assert extract_vault_yaml_from_git() == _ALPHA_VAULT.decode("utf-8")

    @pytest.mark.parametrize("missing", ["HEAD:vault.yaml", "HEAD"], ids=["blob", "commit"])
    def test_a_missing_object_raises_instead_of_reading_as_absent(self, tmp_path, monkeypatch,
                                                                   missing):
        from schist.pre_receive import extract_vault_yaml_from_git
        repo, _ = _bare_repo_with(tmp_path, [{"vault.yaml": _ALPHA_VAULT}])
        _loosen(repo)
        _drop_object(repo, missing)
        monkeypatch.chdir(repo)
        with pytest.raises(subprocess.CalledProcessError):
            extract_vault_yaml_from_git()

    def test_a_git_timeout_raises(self, tmp_path, monkeypatch):
        from schist.pre_receive import extract_vault_yaml_from_git
        repo, _ = _bare_repo_with(tmp_path, [{"vault.yaml": _ALPHA_VAULT}])
        monkeypatch.chdir(repo)
        real_run = subprocess.run

        def slow_cat_file(args, *a, **k):
            if list(args[:2]) == ["git", "cat-file"]:
                raise subprocess.TimeoutExpired(args, 10)
            return real_run(args, *a, **k)

        monkeypatch.setattr(subprocess, "run", slow_cat_file)
        with pytest.raises(subprocess.TimeoutExpired):
            extract_vault_yaml_from_git()

    def test_main_refuses_a_push_when_vault_yaml_cannot_be_read(self, tmp_path, monkeypatch,
                                                                capsys):
        """The push a scoped writer could land while the blob was unreadable:
        a file outside its scope."""
        from schist.pre_receive import main
        repo, (old, new) = _bare_repo_with(tmp_path, [{"vault.yaml": _ALPHA_VAULT},
                                                      {"outside.md": b"o\n"}])
        _loosen(repo)
        _drop_object(repo, "HEAD:vault.yaml")
        monkeypatch.chdir(repo)
        rc = main(stdin=[f"{old} {new} refs/heads/main"], identity="alpha",
                  log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        err = capsys.readouterr().err
        # git ran and failed (a missing object): no retry fixes it, so it is a
        # refusal, not the transient ERROR a timeout gets.
        assert err.startswith("REJECTED: failed to load vault.yaml: git exited 128 reading it")

    def test_a_vault_yaml_timeout_asks_for_a_retry(self, tmp_path, monkeypatch, capsys):
        from schist.pre_receive import main
        def slow(git_dir=None):
            raise subprocess.TimeoutExpired(["git", "cat-file"], 10)
        monkeypatch.setattr("schist.pre_receive.extract_vault_yaml_from_git", slow)
        monkeypatch.chdir(tmp_path)
        rc = main(stdin=["a b refs/heads/main"], identity="alpha",
                  log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        assert capsys.readouterr().err.startswith(
            "ERROR: the hub could not read vault.yaml (git timed out); please retry the push.")

    def test_a_git_killed_by_a_signal_asks_for_a_retry(self, tmp_path, monkeypatch, capsys):
        """A git the OOM killer ended (under the memory limit the docs
        recommend) said nothing about the repository: "git exited -9; run
        git fsck" sent the admin after a broken object that did not exist,
        and spokes stopped retrying."""
        from schist.pre_receive import _git_failure, main
        killed = subprocess.CalledProcessError(-9, ["git", "cat-file"])
        assert _git_failure(killed) == "git was killed by signal 9"
        assert _git_failure(subprocess.CalledProcessError(128, ["git"])) == "git exited 128"

        def oom(git_dir=None):
            raise killed
        monkeypatch.setattr("schist.pre_receive.extract_vault_yaml_from_git", oom)
        monkeypatch.chdir(tmp_path)
        rc = main(stdin=["a b refs/heads/main"], identity="alpha",
                  log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        assert capsys.readouterr().err == (
            "ERROR: the hub could not read vault.yaml (git was killed by signal 9); "
            "please retry the push.\n")

    def test_head_naming_a_missing_branch_beside_other_refs_raises(self, tmp_path,
                                                                   monkeypatch):
        """Not an empty hub: the served branch was renamed or HEAD names
        another branch, and the vault.yaml on `main` still governs."""
        from schist.pre_receive import extract_vault_yaml_from_git
        repo, _ = _bare_repo_with(tmp_path, [{"vault.yaml": _ALPHA_VAULT}])
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/trunk"], cwd=repo, check=True)
        monkeypatch.chdir(repo)
        with pytest.raises(RuntimeError, match="HEAD names refs/heads/trunk, which does not exist"):
            extract_vault_yaml_from_git()

    def test_main_refuses_a_push_while_head_names_a_missing_branch(self, tmp_path, monkeypatch,
                                                                  capsys):
        from schist.pre_receive import main
        repo, (old, new) = _bare_repo_with(tmp_path, [{"vault.yaml": _ALPHA_VAULT},
                                                      {"outside.md": b"o\n"}])
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/trunk"], cwd=repo, check=True)
        monkeypatch.chdir(repo)
        rc = main(stdin=[f"{old} {new} refs/heads/main"], identity="alpha",
                  log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
        assert rc == 1
        err = capsys.readouterr().err
        assert err.startswith("REJECTED: failed to load vault.yaml: HEAD names refs/heads/trunk")
        assert "git symbolic-ref HEAD refs/heads/<branch>" in err

    def test_a_vault_yaml_that_is_not_utf8_is_refused_with_its_cause(self, tmp_path, monkeypatch,
                                                                      capsys):
        from schist.pre_receive import main
        latin1 = _ALPHA_VAULT + "# café\n".encode("latin-1")
        repo, (old, new) = _bare_repo_with(tmp_path, [{"vault.yaml": latin1},
                                                      {"research/alpha/n.md": b"n\n"}])
        monkeypatch.chdir(repo)
        rc = main(stdin=[f"{old} {new} refs/heads/main"], identity="alpha",
                  log_path=tmp_path / "rejected.log", db_path=tmp_path / "rl.sqlite")
        err = capsys.readouterr().err
        assert rc == 1
        assert ("not valid UTF-8, so every push is refused until a hub admin rewrites it "
                "as UTF-8 (docs/hub-spoke-setup.md") in err


SCOPE = "research/ma" + chr(0xEF) + "n"                      # research/maïn
MOJIBAKE = "research/ma" + chr(0xC3) + chr(0xAF) + "n"       # its UTF-8 bytes read as latin-9

_MAIN_IN_HUB = """
import sys
from pathlib import Path
from schist.pre_receive import main
old, new, tmp = sys.argv[1:4]
sys.exit(main(stdin=[old + " " + new + " refs/heads/main"], identity="alpha",
              log_path=Path(tmp) / "rejected.log", db_path=Path(tmp) / "rl.sqlite"))
"""


@pytest.mark.parametrize("locale_name", HOOK_LOCALES)
@pytest.mark.parametrize("pushed, accepted", [(SCOPE, True), (MOJIBAKE, False)],
                         ids=["granted-scope", "lookalike-scope"])
def test_vault_yaml_scopes_read_the_same_under_any_locale(tmp_path, monkeypatch, locale_name,
                                                          pushed, accepted):
    """vault.yaml grants `research/maïn`. Read by a latin-9 locale it named
    `research/maÃ¯n` instead, so a push there landed and one to the granted
    directory was refused."""
    monkeypatch.delenv("GIT_DIR", raising=False)
    vault = ("name: v\nvault_version: 1\nscope_convention: subdirectory\n"
             "participants:\n  - {name: alpha, type: agent}\n"
             f"access:\n  alpha: {{read: ['*'], write: ['{SCOPE}']}}\n").encode("utf-8")
    repo, (old, new) = _bare_repo_with(tmp_path, [
        {"vault.yaml": vault}, {pushed + "/n.md": b"---\ntitle: n\n---\n"}])
    r = _run_in_locale(locale_name, repo, _MAIN_IN_HUB, old, new, str(tmp_path))
    assert (r.returncode == 0) is accepted, r.stderr
    assert "Traceback" not in r.stderr


_FIND_BAD = """
import sys
from schist.pre_receive import find_bad_frontmatter, get_changed_files
old, new = sys.argv[1:3]
print(ascii(find_bad_frontmatter(old, new, get_changed_files(old, new))))
"""


@pytest.mark.parametrize("locale_name", HOOK_LOCALES)
def test_a_note_whose_path_holds_a_line_break_is_read_under_any_locale(tmp_path, monkeypatch,
                                                                       locale_name):
    """A path holding a line break is read with `git cat-file blob <rev>:<path>`.
    A str argument is encoded with the locale, so under latin-9 a non-ASCII
    path named no blob and a `---js` note went unread."""
    monkeypatch.delenv("GIT_DIR", raising=False)
    repo = tmp_path / "hub.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(repo)], check=True)
    quoted = b'"research/mario/x\\n\\303\\255.md"'     # research/mario/x<LF>í.md
    stream = (b"commit refs/heads/main\ncommitter t <t@example.com> 1700000000 +0000\n"
              b"data 2\nc\nM 100644 inline research/mario/seed.md\ndata 2\nx\n\n"
              b"commit refs/heads/main\ncommitter t <t@example.com> 1700000001 +0000\n"
              b"data 2\nc\nM 100644 inline " + quoted + b"\ndata 13\n---js\n{}\n---\n\n")
    subprocess.run(["git", "fast-import", "--quiet"], cwd=repo, input=stream, check=True)
    old, new = subprocess.run(["git", "rev-list", "--reverse", "main"], cwd=repo,
                              capture_output=True, text=True, check=True).stdout.split()
    r = _run_in_locale(locale_name, repo, _FIND_BAD, old, new)
    assert r.stdout.strip() == ascii(["research/mario/x\n" + chr(0xED) + ".md"]), r.stderr
