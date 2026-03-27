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
