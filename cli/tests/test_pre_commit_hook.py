"""Corpus tests for the pre-commit secret-detection regex (issue #103).

The hook ships as a shell template baked into `cli/schist/sync.py`. These tests
install it into a real git repo and confirm the staged-secret guard accepts
benign content (false positives that previously burned users) and still rejects
real-looking secrets.
"""

from __future__ import annotations

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


def test_hook_carries_version_marker() -> None:
    """Doctor's freshness check parses this marker — it must be present."""
    assert f"# schist-hook-version: {HOOK_VERSION}" in PRE_COMMIT_HOOK


def test_hook_version_is_an_int() -> None:
    """`schist doctor` compares HOOK_VERSION as a token string but the constant
    should be an int so bumps are unambiguous."""
    assert isinstance(HOOK_VERSION, int)
    assert HOOK_VERSION >= 2  # was 1 (unversioned) before issue #103
