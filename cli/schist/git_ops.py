"""Git operations for vault commits."""

import subprocess


def commit(vault_path: str, message: str, files: list[str] | None = None) -> tuple[bool, str]:
    """Stage and commit in the vault repo."""
    try:
        add_args = files if files else ['.']
        subprocess.run(
            ['git', 'add'] + add_args,
            cwd=vault_path, check=True, capture_output=True, text=True,
        )
        result = subprocess.run(
            ['git', 'commit', '-m', message],
            cwd=vault_path, capture_output=True, text=True,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output.strip()
    except subprocess.CalledProcessError as e:
        return False, (e.stdout or '') + (e.stderr or '')


def current_branch(vault_path: str) -> str:
    """Return current git branch name."""
    result = subprocess.run(
        ['git', 'branch', '--show-current'],
        cwd=vault_path, capture_output=True, text=True,
    )
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
        subprocess.run(
            ['git', 'sparse-checkout', 'init', '--cone'],
            cwd=vault_path, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ['git', 'sparse-checkout', 'set', scope],
            cwd=vault_path, check=True, capture_output=True, text=True,
        )
        result = subprocess.run(
            ['git', 'checkout'],
            cwd=vault_path, capture_output=True, text=True,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output.strip()
    except subprocess.CalledProcessError as e:
        return False, (e.stdout or '') + (e.stderr or '')


def pull_rebase(vault_path: str) -> tuple[bool, str]:
    """Pull with rebase from origin. Aborts rebase on conflict."""
    try:
        branch = current_branch(vault_path)
        result = subprocess.run(
            ['git', 'pull', '--rebase', 'origin', branch],
            cwd=vault_path, capture_output=True, text=True, timeout=60,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            # Abort the failed rebase to restore clean state
            subprocess.run(
                ['git', 'rebase', '--abort'],
                cwd=vault_path, capture_output=True, text=True,
            )
            return False, output.strip()
        return True, output.strip()
    except subprocess.TimeoutExpired:
        # Abort any in-progress rebase to restore clean state
        subprocess.run(
            ['git', 'rebase', '--abort'],
            cwd=vault_path, capture_output=True, text=True,
        )
        return False, "Pull timed out after 60s"
    except subprocess.CalledProcessError as e:
        return False, (e.stdout or '') + (e.stderr or '')


def push(vault_path: str) -> tuple[bool, str]:
    """Push current branch to origin."""
    try:
        branch = current_branch(vault_path)
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
    """Check for staged or unstaged changes."""
    result = subprocess.run(
        ['git', 'status', '--porcelain'],
        cwd=vault_path, capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def has_unpushed_commits(vault_path: str) -> bool:
    """Check if local branch is ahead of origin.

    Returns True if there are unpushed commits, or if the check fails
    (erring on the side of attempting a push).
    """
    branch = current_branch(vault_path)
    if not branch:
        return True  # Detached HEAD — let push attempt and report the real error
    result = subprocess.run(
        ['git', 'rev-list', f'origin/{branch}..HEAD', '--count'],
        cwd=vault_path, capture_output=True, text=True,
    )
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
        result = subprocess.run(
            ['git', 'add', scope + '/'],
            cwd=vault_path, capture_output=True, text=True,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output.strip()
    except subprocess.CalledProcessError as e:
        return False, (e.stdout or '') + (e.stderr or '')
