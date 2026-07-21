"""Corpus tests for the pre-commit secret-detection regex (issue #103).

The hook ships as a shell template baked into `cli/schist/sync.py`. These tests
install it into a real git repo and confirm the staged-secret guard accepts
benign content (false positives that previously burned users) and still rejects
real-looking secrets.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from schist.sync import PRE_COMMIT_HOOK, HOOK_VERSION


# Strings that previously tripped the hook even though they contain no secret.
# Each is something the user reported in #103 or is a representative analogue.
SHOULD_NOT_MATCH = [
    "risk-hedge",
    "concepts/2026-05-18-brain-state-vs-task-context-terminology.md",
    "task-some-long-concept-slug-with-dashes",
    "task-vs-task",
    "-----BEGIN draft outline-----",
    "-----BEGIN OUTLINE-----",
    "set `password = <your-password>` in your env file",
    "the api_key = <placeholder> example in the docs",
]

# Strings the hook MUST reject. If any of these silently pass, the secret guard
# has lost coverage of a real attack surface.
SHOULD_MATCH = [
    "sk-proj-abcdef0123456789abcdef0123456789",
    "API_TOKEN=sk-proj-abcdef0123456789abcdef0123456789",
    "ghp_1234567890abcdefABCDEF1234567890abcdef",
    "ghs_1234567890abcdefABCDEF1234567890abcdef",
    "AKIAIOSFODNN7EXAMPLE",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    'password = "hunter2"',
    "api_key = 'sk-redacted-but-quoted-value-here'",
]


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A git repo with the canonical pre-commit hook installed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(PRE_COMMIT_HOOK)
    hook.chmod(0o755)
    return repo


def _try_commit(vault: Path, filename: str, content: str) -> subprocess.CompletedProcess:
    (vault / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=vault, check=True)
    return subprocess.run(
        ["git", "commit", "-m", "test"],
        cwd=vault, capture_output=True, text=True,
    )


@pytest.mark.parametrize("content", SHOULD_NOT_MATCH)
def test_benign_content_commits(vault: Path, content: str) -> None:
    """Free text and doc prose must not trigger the secret guard."""
    result = _try_commit(vault, "note.md", content + "\n")
    assert result.returncode == 0, (
        f"Hook falsely rejected benign content {content!r}: {result.stdout}{result.stderr}"
    )


@pytest.mark.parametrize("content", SHOULD_MATCH)
def test_real_secrets_blocked(vault: Path, content: str) -> None:
    """Recognizable secret formats must still be rejected."""
    result = _try_commit(vault, "secret.md", content + "\n")
    assert result.returncode != 0, (
        f"Hook silently accepted secret pattern {content!r}"
    )
    assert "Potential secret detected" in result.stdout + result.stderr


def test_secret_in_filename_with_spaces_is_blocked(vault: Path) -> None:
    """Whitespace in staged paths must not let files bypass secret scanning."""
    result = _try_commit(
        vault,
        "research note with spaces.md",
        "api_key = 'sk-redacted-but-quoted-value-here'\n",
    )
    assert result.returncode != 0, (
        "Hook silently accepted a secret in a staged filename containing spaces"
    )
    output = result.stdout + result.stderr
    assert "Potential secret detected" in output
    assert "research note with spaces.md" in output


def test_secret_in_filename_with_single_quote_is_blocked(vault: Path) -> None:
    """A quote in a staged path must not break the scanner's argument passing
    (#279). Filenames reach the inner sh as argv slots, never as script text."""
    result = _try_commit(
        vault,
        "it's-todo.md",
        "api_key = 'sk-redacted-but-quoted-value-here'\n",
    )
    assert result.returncode != 0, (
        "Hook silently accepted a secret in a staged filename containing a single quote"
    )
    assert "Potential secret detected" in result.stdout + result.stderr


def _long_relpath(vault: Path) -> str:
    """A staged path well past BSD xargs' 255-byte -I replacement buffer."""
    subdir = vault / ("d" * 100) / ("e" * 100)
    subdir.mkdir(parents=True)
    return str((subdir / ("f" * 80 + ".md")).relative_to(vault))


def test_secret_in_long_path_is_blocked(vault: Path) -> None:
    """BSD xargs -I aborts on items longer than its 255-byte replacement
    buffer; the old hook then treated the failed scan as "no match" and let
    the secret through (#279). Long paths must scan like any other."""
    result = _try_commit(
        vault,
        _long_relpath(vault),
        "api_key = 'sk-redacted-but-quoted-value-here'\n",
    )
    assert result.returncode != 0, (
        "Hook silently accepted a secret in a staged path longer than 255 bytes"
    )
    assert "Potential secret detected" in result.stdout + result.stderr


def test_benign_long_path_commits(vault: Path) -> None:
    """The long-path fix must not fail closed on clean files."""
    result = _try_commit(vault, _long_relpath(vault), "just a note\n")
    assert result.returncode == 0, (
        f"Hook rejected a benign file at a long path: {result.stdout}{result.stderr}"
    )


def test_missing_inherited_tmpdir_falls_back_to_system_tmp(vault: Path, tmp_path: Path) -> None:
    """A stale sandbox TMPDIR must not make otherwise-valid commits fail."""
    missing_tmpdir = tmp_path / "removed-sandbox-tmp"
    (vault / "note.md").write_text("just a note\n")
    subprocess.run(["git", "add", "note.md"], cwd=vault, check=True)

    env = os.environ.copy()
    env["TMPDIR"] = str(missing_tmpdir)
    result = subprocess.run(
        ["git", "commit", "-m", "test"],
        cwd=vault,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, (
        f"Hook trusted a missing inherited TMPDIR: {result.stdout}{result.stderr}"
    )
    assert not missing_tmpdir.exists(), "hook must not create an untrusted TMPDIR"


def test_staged_secret_blocked_after_worktree_cleanup(vault: Path) -> None:
    """The hook must inspect the staged blob, not the working-tree file."""
    note = vault / "secret.md"
    note.write_text("api_key = 'sk-redacted-but-quoted-value-here'\n")
    subprocess.run(["git", "add", "secret.md"], cwd=vault, check=True)

    note.write_text("clean working-tree copy\n")
    result = subprocess.run(
        ["git", "commit", "-m", "test"],
        cwd=vault, capture_output=True, text=True,
    )

    assert result.returncode != 0, (
        "Hook silently accepted a secret that remained in the staged blob"
    )
    output = result.stdout + result.stderr
    assert "Potential secret detected" in output
    assert "secret.md" in output


def test_pre_commit_hook_carries_version_marker() -> None:
    """Doctor's freshness check parses this marker — it must be present."""
    assert f"# schist-hook-version: {HOOK_VERSION}" in PRE_COMMIT_HOOK


def test_post_commit_hook_carries_version_marker() -> None:
    """Both hook templates carry a version marker; bumping HOOK_VERSION must
    update both literals in lockstep. This test catches drift."""
    from schist.sync import POST_COMMIT_HOOK

    assert f"# schist-hook-version: {HOOK_VERSION}" in POST_COMMIT_HOOK


def test_hook_version_is_an_int() -> None:
    """`schist doctor` compares HOOK_VERSION as a token string but the constant
    should be an int so bumps are unambiguous."""
    assert isinstance(HOOK_VERSION, int)
    assert HOOK_VERSION >= 3  # was 2 before issue #202 fixed path splitting


class TestHooksReinstall:
    """Direct tests for the `schist hooks reinstall` command path.

    The end-to-end CLI is exercised via __main__, but the sync.hooks_reinstall
    function is the unit under test — covers happy path, missing-.git error,
    pinned-skip, --force override, and the atomic-rename invariant."""

    def _vault(self, tmp_path: Path) -> Path:
        v = tmp_path / "v"
        v.mkdir()
        (v / ".git").mkdir()
        (v / ".git" / "hooks").mkdir()
        return v

    def _args(self, force: bool = False):
        from argparse import Namespace
        return Namespace(force=force)

    def test_happy_path_writes_both_hooks(self, tmp_path: Path) -> None:
        from schist.sync import hooks_reinstall, PRE_COMMIT_HOOK, POST_COMMIT_HOOK

        v = self._vault(tmp_path)
        hooks_reinstall(self._args(), str(v), "")
        assert (v / ".git" / "hooks" / "pre-commit").read_text() == PRE_COMMIT_HOOK
        assert (v / ".git" / "hooks" / "post-commit").read_text() == POST_COMMIT_HOOK
        # Atomic-rename leaves no stray .tmp siblings.
        leftover = list((v / ".git" / "hooks").glob("*.tmp"))
        assert leftover == [], f"unexpected .tmp leftovers: {leftover}"

    def test_not_a_git_repo_exits(self, tmp_path: Path) -> None:
        from schist.sync import hooks_reinstall

        with pytest.raises(SystemExit) as exc:
            hooks_reinstall(self._args(), str(tmp_path), "")
        assert exc.value.code == 1

    def test_pinned_hook_is_skipped(self, tmp_path: Path, capsys) -> None:
        from schist.sync import hooks_reinstall, PRE_COMMIT_HOOK

        v = self._vault(tmp_path)
        pinned_body = "#!/bin/sh\n# schist-hook-version: pinned\n# my custom guard\nexit 0\n"
        (v / ".git" / "hooks" / "pre-commit").write_text(pinned_body)

        hooks_reinstall(self._args(force=False), str(v), "")

        # pre-commit untouched
        assert (v / ".git" / "hooks" / "pre-commit").read_text() == pinned_body
        # post-commit refreshed
        from schist.sync import POST_COMMIT_HOOK
        assert (v / ".git" / "hooks" / "post-commit").read_text() == POST_COMMIT_HOOK
        # User sees the skip notice
        err = capsys.readouterr().err
        assert "Skipped pre-commit" in err
        assert "pinned" in err
        assert "--force" in err

    def test_force_overwrites_pinned(self, tmp_path: Path) -> None:
        from schist.sync import hooks_reinstall, PRE_COMMIT_HOOK

        v = self._vault(tmp_path)
        (v / ".git" / "hooks" / "pre-commit").write_text(
            "#!/bin/sh\n# schist-hook-version: pinned\nexit 0\n"
        )
        hooks_reinstall(self._args(force=True), str(v), "")
        assert (v / ".git" / "hooks" / "pre-commit").read_text() == PRE_COMMIT_HOOK


def test_version_marker_with_trailing_comment_parses() -> None:
    """Users sometimes annotate the marker — doctor's regex must tolerate
    `# schist-hook-version: 2 # bumped 2026-05-19` rather than collapsing to
    'legacy'."""
    from schist.doctor import _installed_hook_version
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write("#!/bin/sh\n# schist-hook-version: 2  # bumped 2026-05-19\nexit 0\n")
        path = Path(f.name)
    try:
        assert _installed_hook_version(path) == "2"
    finally:
        path.unlink()


def test_unreadable_hook_raises_distinct_error(tmp_path: Path) -> None:
    """A permission-locked hook must NOT collapse into 'legacy' — that would
    suggest the user run `hooks reinstall`, which would also fail with the
    same permission error. doctor.HookReadError keeps the failure visible."""
    from schist.doctor import _installed_hook_version, HookReadError

    hook = tmp_path / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o000)  # unreadable

    try:
        with pytest.raises(HookReadError):
            _installed_hook_version(hook)
    finally:
        hook.chmod(0o644)  # so pytest can clean up


def test_missing_hook_returns_none() -> None:
    """FileNotFoundError stays distinct from PermissionError — missing hook
    is None (check_post_commit_hook reports it separately), not a HookReadError."""
    from schist.doctor import _installed_hook_version

    assert _installed_hook_version(Path("/nonexistent/hook")) is None


def test_secret_in_colon_prefixed_filename_is_blocked(vault: Path) -> None:
    """A file named "0:evil.md" makes the bare ":<path>" pathspec parse as
    "stage 0 of evil.md" — git errors (file skipped, secret through) or, with
    a clean sibling evil.md staged, scans the WRONG blob. The explicit
    ":0:<path>" stage form takes the path literally."""
    result = _try_commit(
        vault,
        "0:evil.md",
        "api_key = 'sk-redacted-but-quoted-value-here'\n",
    )
    assert result.returncode != 0, (
        "Hook silently accepted a secret in a staged filename starting with '0:'"
    )
    assert "Potential secret detected" in result.stdout + result.stderr


def test_secret_not_masked_by_clean_sibling_blob(vault: Path) -> None:
    """Wrong-blob variant: with a clean "x.md" staged, the bare pathspec for
    "0:x.md" resolved to x.md's clean blob and the secret passed unseen."""
    (vault / "x.md").write_text("clean sibling\n")
    subprocess.run(["git", "add", "x.md"], cwd=vault, check=True)
    result = _try_commit(
        vault,
        "0:x.md",
        "api_key = 'sk-redacted-but-quoted-value-here'\n",
    )
    assert result.returncode != 0, (
        "Hook scanned the clean sibling blob instead of the staged '0:x.md'"
    )
    output = result.stdout + result.stderr
    assert "Potential secret detected" in output
    assert "0:x.md" in output
