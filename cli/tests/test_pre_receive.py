"""Tests for the git pre-receive hook ACL enforcement."""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from schist.acl import VaultACL, parse_vault_data
from schist.pre_receive import (
    Violation,
    check_push,
    derive_scope,
    format_rejection,
    log_rejection,
    main,
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

    def test_empty_stdin(self, acl):
        rc = self._run(acl, "admin", [""], [])
        assert rc == 0

    def test_multiple_refs(self, acl, tmp_path):
        """Multiple ref updates in a single push."""
        stdin = [
            "aaa bbb refs/heads/main",
            "ccc ddd refs/heads/feature",
        ]
        # Both refs change only in-scope files
        with patch("schist.pre_receive.get_changed_files", return_value=["research/mario/note.md"]):
            rc = main(stdin=stdin, acl=acl, identity="cluster-mario")
        assert rc == 0

    def test_multiple_refs_one_bad(self, acl, tmp_path, capsys):
        """One ref is fine, another has out-of-scope files."""
        log_path = tmp_path / "rejected-pushes.log"
        call_count = [0]
        files_per_call = [
            ["research/mario/ok.md"],
            ["security/bad.md"],
        ]

        def mock_changed_files(oldrev, newrev):
            idx = call_count[0]
            call_count[0] += 1
            return files_per_call[idx]

        stdin = [
            "aaa bbb refs/heads/main",
            "ccc ddd refs/heads/feature",
        ]
        with patch("schist.pre_receive.get_changed_files", side_effect=mock_changed_files):
            rc = main(stdin=stdin, acl=acl, identity="cluster-mario", log_path=log_path)
        assert rc == 1

    def test_deleted_branch_allowed(self, acl):
        """Deleting a branch (newrev=0*40) should always be allowed."""
        zero = "0" * 40
        stdin = [f"abc123 {zero} refs/heads/old-branch"]
        with patch("schist.pre_receive.get_changed_files", return_value=[]):
            rc = main(stdin=stdin, acl=acl, identity="cluster-mario")
        assert rc == 0

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


class TestGetChangedFiles:
    def test_deleted_branch_returns_empty(self):
        from schist.pre_receive import ZERO_SHA, get_changed_files

        result = get_changed_files("abc123", ZERO_SHA)
        assert result == []

    def test_normal_diff(self):
        from schist.pre_receive import get_changed_files

        mock_result = type("Result", (), {"stdout": "a.md\0b.md\0", "returncode": 0})()
        with patch("subprocess.run", return_value=mock_result):
            result = get_changed_files("aaa", "bbb")
        assert result == ["a.md", "b.md"]

    def test_new_branch_uses_diff_tree(self):
        from schist.pre_receive import ZERO_SHA, get_changed_files

        mock_result = type("Result", (), {"stdout": "new-file.md\0", "returncode": 0})()
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = get_changed_files(ZERO_SHA, "abc123")
        assert result == ["new-file.md"]
        # Should use diff-tree for new branches
        cmd = mock_run.call_args[0][0]
        assert "diff-tree" in cmd

    def test_empty_diff_returns_empty(self):
        from schist.pre_receive import get_changed_files

        mock_result = type("Result", (), {"stdout": "", "returncode": 0})()
        with patch("subprocess.run", return_value=mock_result):
            result = get_changed_files("aaa", "bbb")
        assert result == []


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
