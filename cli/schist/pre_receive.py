"""Git pre-receive hook for enforcing vault.yaml ACL on pushes.

Validates that each pushed file is within the pusher's write scope.
Identity is resolved from environment variables (SCHIST_IDENTITY or GL_USER).

Usage as a git hook:
    GIT_DIR/hooks/pre-receive calls this module's main().
    stdin provides lines of: oldrev newrev refname
"""

from __future__ import annotations

import datetime
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from schist.acl import VaultACL, parse_vault_yaml

logger = logging.getLogger("schist.pre_receive")

# SHA representing a zero ref (new branch or deleted branch)
ZERO_SHA = "0" * 40


@dataclass
class Violation:
    """A single ACL violation: a file the identity cannot write."""

    identity: str
    filepath: str
    scope: str
    refname: str


def resolve_identity() -> str | None:
    """Resolve the pushing identity from environment variables.

    Priority:
    1. SCHIST_IDENTITY — explicit schist identity
    2. GL_USER — gitolite compatibility
    """
    return os.environ.get("SCHIST_IDENTITY") or os.environ.get("GL_USER") or None


def derive_scope(filepath: str) -> str:
    """Derive the ACL scope from a file path.

    For scope_convention=subdirectory:
    - research/mario/note.md  → research/mario
    - research/note.md        → research
    - vault.yaml              → (root) — empty string means root-level file

    The returned scope is the directory portion of the path, normalized
    to prevent path traversal attacks (e.g. research/mario/../hbcd/).
    Root-level files return empty string, which requires "*" write access.
    """
    # normpath resolves ".." segments without touching the filesystem
    normalized = os.path.normpath(filepath)
    parent = str(Path(normalized).parent)
    if parent == ".":
        return ""
    return parent


def get_changed_files(oldrev: str, newrev: str) -> list[str]:
    """Get list of files changed between two revisions.

    Handles special cases:
    - New branch (oldrev is zero): diff against empty tree
    - Deleted branch (newrev is zero): no files to check
    """
    if newrev == ZERO_SHA:
        # Branch deletion — nothing to check
        return []

    if oldrev == ZERO_SHA:
        # New branch — diff all files in the new ref against empty tree
        # Use git diff-tree against the empty tree hash
        result = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", newrev],
            capture_output=True,
            text=True,
            check=True,
        )
    else:
        result = subprocess.run(
            ["git", "diff", "--name-only", oldrev, newrev],
            capture_output=True,
            text=True,
            check=True,
        )

    return [f for f in result.stdout.strip().split("\n") if f]


def check_push(
    identity: str,
    changed_files: list[str],
    acl: VaultACL,
    refname: str,
) -> list[Violation]:
    """Check all changed files against the identity's write ACL.

    Returns a list of Violation objects for any out-of-scope writes.
    """
    violations: list[Violation] = []

    for filepath in changed_files:
        scope = derive_scope(filepath)

        if scope == "":
            # Root-level file — only wildcard writers can modify
            if not acl.can_write(identity, "__root__"):
                # can_write with a non-matching scope; check if they have "*"
                entry = acl.access.get(identity)
                if entry is None or "*" not in entry.write:
                    violations.append(Violation(
                        identity=identity,
                        filepath=filepath,
                        scope="(root)",
                        refname=refname,
                    ))
        else:
            if not acl.can_write(identity, scope):
                violations.append(Violation(
                    identity=identity,
                    filepath=filepath,
                    scope=scope,
                    refname=refname,
                ))

    return violations


def format_rejection(violations: list[Violation]) -> str:
    """Format a human-readable rejection message."""
    lines = [
        "REJECTED: push contains out-of-scope writes",
        f"Identity: {violations[0].identity}",
        "",
        "Violations:",
    ]
    for v in violations:
        lines.append(f"  - {v.filepath} (scope: {v.scope})")
    lines.append("")
    lines.append("Check vault.yaml access rules for your identity.")
    return "\n".join(lines)


def log_rejection(
    violations: list[Violation],
    log_path: Path | None = None,
) -> None:
    """Append rejection details to the audit log."""
    if log_path is None:
        # Default: alongside the hook in GIT_DIR/hooks/
        git_dir = os.environ.get("GIT_DIR", ".")
        log_path = Path(git_dir) / "hooks" / "rejected-pushes.log"

    timestamp = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
    identity = violations[0].identity
    refname = violations[0].refname
    files = ", ".join(v.filepath for v in violations)

    entry = f"[{timestamp}] REJECTED identity={identity} ref={refname} files=[{files}]\n"

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(entry)
    except OSError as e:
        logger.warning("Failed to write rejection log: %s", e)


def find_vault_yaml() -> Path | None:
    """Locate vault.yaml in the repository being pushed to.

    In a bare repo (typical for pre-receive), vault.yaml is accessed via
    git show HEAD:vault.yaml. For non-bare repos, check the working tree.
    """
    git_dir = os.environ.get("GIT_DIR", ".")

    # Try working tree first (non-bare repo)
    work_tree = Path(git_dir).parent / "vault.yaml"
    if work_tree.is_file():
        return work_tree

    # Bare repo — check if vault.yaml exists at HEAD
    bare_path = Path(git_dir) / "vault.yaml"
    if bare_path.is_file():
        return bare_path

    return None


def extract_vault_yaml_from_git() -> str | None:
    """Extract vault.yaml content from HEAD in a bare repository."""
    try:
        result = subprocess.run(
            ["git", "show", "HEAD:vault.yaml"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError:
        return None


def load_acl() -> VaultACL | None:
    """Load vault.yaml ACL, trying filesystem first, then git show."""
    path = find_vault_yaml()
    if path is not None:
        return parse_vault_yaml(path)

    # Bare repo fallback: extract from git
    content = extract_vault_yaml_from_git()
    if content is not None:
        import yaml

        from schist.acl import parse_vault_data

        data = yaml.safe_load(content)
        return parse_vault_data(data)

    return None


def main(
    stdin: list[str] | None = None,
    acl: VaultACL | None = None,
    identity: str | None = None,
    log_path: Path | None = None,
) -> int:
    """Pre-receive hook entry point.

    Args:
        stdin: Lines from stdin (oldrev newrev refname). None reads sys.stdin.
        acl: Pre-loaded VaultACL. None loads from vault.yaml.
        identity: Override identity. None resolves from environment.
        log_path: Override rejection log path.

    Returns:
        0 if push is allowed, 1 if rejected.
    """
    # Resolve identity
    if identity is None:
        identity = resolve_identity()
    if identity is None:
        print("REJECTED: cannot determine push identity", file=sys.stderr)
        print(
            "Set SCHIST_IDENTITY or GL_USER environment variable.",
            file=sys.stderr,
        )
        return 1

    # Load ACL
    if acl is None:
        try:
            acl = load_acl()
        except Exception as e:
            print(f"REJECTED: failed to load vault.yaml: {e}", file=sys.stderr)
            return 1

    if acl is None:
        # No vault.yaml — allow push (vault.yaml is optional per spec)
        return 0

    # Verify identity is a known participant
    if acl.get_participant(identity) is None:
        print(
            f"REJECTED: unknown identity '{identity}' — "
            "not listed in vault.yaml participants",
            file=sys.stderr,
        )
        return 1

    # Read push refs from stdin
    if stdin is None:
        stdin = sys.stdin.read().strip().split("\n")

    all_violations: list[Violation] = []

    for line in stdin:
        line = line.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) < 3:
            continue

        oldrev, newrev, refname = parts[0], parts[1], parts[2]

        try:
            changed_files = get_changed_files(oldrev, newrev)
        except subprocess.CalledProcessError as e:
            print(f"ERROR: failed to diff {oldrev}..{newrev}: {e}", file=sys.stderr)
            return 1

        violations = check_push(identity, changed_files, acl, refname)
        all_violations.extend(violations)

    if all_violations:
        msg = format_rejection(all_violations)
        print(msg, file=sys.stderr)
        log_rejection(all_violations, log_path=log_path)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
