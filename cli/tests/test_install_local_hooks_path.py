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


def test_hooks_path_naming_own_hooks_dir_does_not_warn(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch, None)
    own = repo / ".git" / "hooks"
    for value in (str(own), ".git/hooks"):
        subprocess.run(
            ["git", "-C", str(repo), "config", "core.hooksPath", value], check=True
        )
        assert sync._install_local_hooks(str(repo)) is None, value


def test_standalone_init_prints_warning_and_succeeds(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    shared = tmp_path / "shared-hooks"
    shared.mkdir()
    cfg = tmp_path / "gitconfig"
    cfg.write_text(f"[core]\n\thooksPath = {shared}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    target = tmp_path / "v"
    sync.init_standalone(SimpleNamespace(path=str(target), name="v", identity="local"))
    err = capsys.readouterr().err
    assert "Warning: core.hooksPath is set to" in err and str(shared) in err
    assert (target / ".git" / "hooks" / "pre-commit").exists()
    assert list(shared.iterdir()) == []


def test_spoke_init_prints_warning(tmp_path, capsys):
    from types import SimpleNamespace
    from unittest.mock import patch

    dest = tmp_path / "spoke"

    def clone(hub_url, dest_path, *a, **kw):
        Path(dest_path).mkdir(parents=True, exist_ok=True)
        (Path(dest_path) / ".git" / "info").mkdir(parents=True)
        return True, ""

    with patch("schist.sync.git_ops.clone_shallow", side_effect=clone), patch(
        "schist.sync.git_ops.setup_sparse_checkout", return_value=(True, "")
    ), patch("schist.sync._install_local_hooks", return_value="HOOKS-WARNING"):
        sync.init_spoke(
            SimpleNamespace(hub="git@pi:v.git", scope="research/x", identity="c"),
            str(dest),
            str(tmp_path / "db.sqlite"),
        )
    assert "Warning: HOOKS-WARNING" in capsys.readouterr().err
