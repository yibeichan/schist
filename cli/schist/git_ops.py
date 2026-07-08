"""Git operations for vault commits."""

from pathlib import Path
import subprocess

import yaml


GLOBAL_SCOPE_FALLBACK_DIRS = [
    "notes", "papers", "concepts",
    "research", "decisions", "ops", "projects", "logs",
]


def commit(vault_path: str, message: str, files: list[str] | None = None) -> tuple[bool, str]:
    """Stage and commit in the vault repo."""
    try:
        add_args = files if files else ['.']
        subprocess.run(
            ['git', 'add'] + add_args,
            cwd=vault_path, check=True, capture_output=True, text=True,
            timeout=60,
        )
        # `git commit` runs the synchronous post-commit hook (schist-ingest),
        # so it needs a generous ceiling — but it MUST have one. Without it a
        # stalled ingest hangs `schist add`/`link`/`sync push` forever with no
        # way out but kill (#256). Mirrors clone/pull/push, which all set one.
        result = subprocess.run(
            ['git', 'commit', '-m', message],
            cwd=vault_path, capture_output=True, text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output.strip()
    except subprocess.TimeoutExpired as e:
        cmd = e.cmd[1] if isinstance(e.cmd, list) and len(e.cmd) > 1 else 'command'
        return False, f"git {cmd} timed out after {e.timeout}s (post-commit ingest may be stalled)"
    except subprocess.CalledProcessError as e:
        return False, (e.stdout or '') + (e.stderr or '')


def current_branch(vault_path: str) -> str:
    """Return current git branch name.

    Returns '' on timeout — callers already treat empty as detached HEAD
    and fall back conservatively (see has_unpushed_commits). #314.
    """
    try:
        result = subprocess.run(
            ['git', 'branch', '--show-current'],
            cwd=vault_path, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return ''
    return result.stdout.strip()


def clone_shallow(hub_url: str, dest: str, depth: int = 1) -> tuple[bool, str]:
    """Shallow clone from hub with no checkout (for sparse-checkout setup)."""
    try:
        result = subprocess.run(
            ['git', 'clone', f'--depth={depth}', '--no-checkout', hub_url, dest],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output.strip()
    except subprocess.TimeoutExpired:
        return False, "Clone timed out after 120s"
    except subprocess.CalledProcessError as e:
        return False, (e.stdout or '') + (e.stderr or '')


def setup_sparse_checkout(vault_path: str, scope: str) -> tuple[bool, str]:
    """Enable sparse-checkout cone mode and set scope directories.

    Cone mode includes all root-level files automatically, so vault.yaml
    and other root configs are always available.
    """
    try:
        # All three are local index/worktree operations, but each can hang
        # indefinitely on a stale index.lock or an NFS stall (#345) — the
        # spoke-init path then blocks forever with no error. Bound them like
        # every other git call in this module.
        subprocess.run(
            ['git', 'sparse-checkout', 'init', '--cone'],
            cwd=vault_path, check=True, capture_output=True, text=True,
            timeout=30,
        )
        subprocess.run(
            ['git', 'sparse-checkout', 'set', scope],
            cwd=vault_path, check=True, capture_output=True, text=True,
            timeout=30,
        )
        result = subprocess.run(
            ['git', 'checkout'],
            cwd=vault_path, capture_output=True, text=True,
            timeout=30,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output.strip()
    except subprocess.TimeoutExpired as e:
        cmd = ' '.join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd)
        return False, f"{cmd} timed out after {e.timeout}s (stale index.lock or NFS stall?)"
    except subprocess.CalledProcessError as e:
        return False, (e.stdout or '') + (e.stderr or '')


def pull_rebase(vault_path: str) -> tuple[bool, str]:
    """Pull with rebase from origin. Aborts rebase on conflict."""
    try:
        branch = current_branch(vault_path)
        if not branch:
            # '' would be passed straight through as the refspec, and
            # `git pull --rebase origin ''` SUCCEEDS — it fetches the
            # remote's default-branch HEAD and silently rebases the current
            # branch onto it (verified on git 2.50). Fail loudly instead. #325
            return False, (
                "Could not determine current branch (detached HEAD, git failure, or timeout); "
                "if a previous sync --force left HEAD detached, reattach with "
                "'git checkout <branch>' in the vault"
            )
        result = subprocess.run(
            ['git', 'pull', '--rebase', 'origin', branch],
            cwd=vault_path, capture_output=True, text=True, timeout=60,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            # Abort the failed rebase to restore clean state. Best-effort and
            # bounded (#321): if the abort itself stalls (NFS, stale lock),
            # surface the original pull error — sync's cleanup_stale_git_state
            # clears leftover rebase state on the next run.
            try:
                subprocess.run(
                    ['git', 'rebase', '--abort'],
                    cwd=vault_path, capture_output=True, text=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                output += "\n(rebase --abort also timed out after 30s; rerun sync to clean up)"
            return False, output.strip()
        return True, output.strip()
    except subprocess.TimeoutExpired:
        # Abort any in-progress rebase to restore clean state (bounded, #321).
        try:
            subprocess.run(
                ['git', 'rebase', '--abort'],
                cwd=vault_path, capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return False, ("Pull timed out after 60s "
                           "(rebase --abort also timed out; rerun sync to clean up)")
        return False, "Pull timed out after 60s"
    except subprocess.CalledProcessError as e:
        return False, (e.stdout or '') + (e.stderr or '')


def push(vault_path: str) -> tuple[bool, str]:
    """Push current branch to origin."""
    try:
        branch = current_branch(vault_path)
        if not branch:
            # `git push origin ''` fails with the cryptic "fatal: invalid
            # refspec ''" (git 2.50); older gits may treat it as a no-op
            # while sync_push reports success. Name the real cause. #325
            return False, (
                "Could not determine current branch (detached HEAD, git failure, or timeout); "
                "if a previous sync --force left HEAD detached, reattach with "
                "'git checkout <branch>' in the vault"
            )
        result = subprocess.run(
            ['git', 'push', 'origin', branch],
            cwd=vault_path, capture_output=True, text=True, timeout=60,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output.strip()
    except subprocess.TimeoutExpired:
        return False, "Push timed out after 60s"
    except subprocess.CalledProcessError as e:
        return False, (e.stdout or '') + (e.stderr or '')


def has_uncommitted_changes(vault_path: str) -> bool:
    """Check for staged or unstaged changes.

    Returns True on timeout (conservative: the sync path then attempts the
    stage/commit — whose own timeouts surface a real error message — rather
    than silently skipping a push). #314.
    """
    try:
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=vault_path, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return True
    return bool(result.stdout.strip())


def has_unpushed_commits(vault_path: str) -> bool:
    """Check if local branch is ahead of origin.

    Returns True if there are unpushed commits, or if the check fails
    (erring on the side of attempting a push).
    """
    branch = current_branch(vault_path)
    if not branch:
        return True  # Detached HEAD — let push attempt and report the real error
    try:
        result = subprocess.run(
            ['git', 'rev-list', f'origin/{branch}..HEAD', '--count'],
            cwd=vault_path, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return True  # Can't determine (#314) — assume yes so push is attempted
    if result.returncode != 0:
        return True  # Can't determine — assume yes so push is attempted
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return True


def stage_scope_files(vault_path: str, scope: str) -> tuple[bool, str]:
    """Stage all files within the scope directory.

    Only stages files under the scope path. Root-level files (vault.yaml,
    schist.yaml) are NOT staged — spokes should not modify those.
    """
    try:
        if scope == "global":
            targets = _global_scope_targets(vault_path)
            if not targets:
                return True, ""
            add_args = ['git', 'add', '--'] + targets
        else:
            add_args = ['git', 'add', '--', scope.rstrip('/') + '/']

        # timeout matches commit()'s git add (#256/#314): staging is the same
        # index-write operation and stalls the same way on NFS lock contention.
        result = subprocess.run(
            add_args,
            cwd=vault_path, capture_output=True, text=True, timeout=60,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output.strip()
    except subprocess.TimeoutExpired:
        return False, 'git add timed out after 60s (NFS stall or stale index lock?)'
    except subprocess.CalledProcessError as e:
        return False, (e.stdout or '') + (e.stderr or '')


def _global_scope_dirs() -> list[str]:
    """Return canonical content directories for logical `global` scope."""
    default_yaml = Path(__file__).resolve().parent / "default.yaml"
    try:
        raw = yaml.safe_load(default_yaml.read_text(encoding="utf-8"))
        dirs = raw.get("directories") if isinstance(raw, dict) else None
        if isinstance(dirs, dict):
            return [str(v).rstrip("/") for v in dirs.values()]
        if isinstance(dirs, list):
            return [str(v).rstrip("/") for v in dirs]
    except Exception:
        pass
    return GLOBAL_SCOPE_FALLBACK_DIRS


def _global_scope_targets(vault_path: str) -> list[str]:
    targets: list[str] = []
    for d in _global_scope_dirs():
        target = f"{d}/"
        if (Path(vault_path) / d).exists():
            targets.append(target)
            continue
        try:
            tracked = subprocess.run(
                ['git', 'ls-files', '--', target],
                cwd=vault_path, capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            # Skip the stalled directory (#314): a partial target list stages
            # less than everything, which the next push attempt picks up —
            # better than hanging the whole sync path.
            continue
        if tracked.returncode == 0 and tracked.stdout.strip():
            targets.append(target)
    return targets
