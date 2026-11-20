# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""`_install_local_hooks` and core.hooksPath (#669): init must not skip silently."""
from __future__ import annotations

import subprocess
from pathlib import Path

from schist import sync


def _repo(tmp_path: Path, monkeypatch, global_cfg: str | None) -> Path:
    cfg = tmp_path / "gitconfig"
    cfg.write_text(global_cfg or "")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "vault"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def test_no_warning_without_hooks_path(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch, None)
    assert sync._install_local_hooks(str(repo)) is None
    assert (repo / ".git" / "hooks" / "pre-commit").exists()


def test_global_hooks_path_warns_and_leaves_shared_dir_alone(tmp_path, monkeypatch):
    shared = tmp_path / "shared-hooks"
    shared.mkdir()
    repo = _repo(tmp_path, monkeypatch, f"[core]\n\thooksPath = {shared}\n")
    warning = sync._install_local_hooks(str(repo))
    assert warning is not None
    assert "core.hooksPath" in warning and str(shared) in warning
    assert list(shared.iterdir()) == []
    assert (repo / ".git" / "hooks" / "post-commit").exists()


def test_empty_hooks_path_warns(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch, "[core]\n\thooksPath = \n")
    warning = sync._install_local_hooks(str(repo))
    assert warning is not None and "empty" in warning


def test_warning_is_printed_by_init_callers(capsys):
    sync._warn_hooks_not_active("core.hooksPath is set to 'x'")
    assert "Warning: core.hooksPath is set to 'x'" in capsys.readouterr().err
    sync._warn_hooks_not_active(None)
    assert capsys.readouterr().err == ""
