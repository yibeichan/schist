# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""`--vault` and `--db` are accepted after the subcommand as well as before it."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _schist(*args: str, env_vault: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "SCHIST_VAULT_PATH"}
    if env_vault is not None:
        env["SCHIST_VAULT_PATH"] = env_vault
    return subprocess.run([sys.executable, "-m", "schist", *args], capture_output=True,
                          text=True, encoding="utf-8", env=env, timeout=120)


@pytest.fixture
def vaults(tmp_path):
    made = []
    for name in ("alpha", "beta"):
        r = _schist("init", str(tmp_path / name), "--name", name, "--identity", "local")
        assert r.returncode == 0, r.stderr
        made.append(tmp_path / name)
    return made


def _notes(vault: Path) -> list[str]:
    return sorted(p.name for p in (vault / "notes").glob("*.md"))


@pytest.mark.parametrize("argv", [
    ["--vault", "{a}", "add", "--title", "t", "--body", "b"],
    ["add", "--vault", "{a}", "--title", "t", "--body", "b"],
    ["add", "--title", "t", "--body", "b", "--vault", "{a}"],
], ids=["before", "after", "last"])
def test_add_writes_to_the_vault_named_either_side(vaults, argv):
    alpha, beta = vaults
    r = _schist(*[a.format(a=alpha) for a in argv])
    assert r.returncode == 0, r.stderr
    assert len(_notes(alpha)) == 1 and _notes(beta) == []


def test_the_option_after_the_subcommand_wins(vaults):
    alpha, beta = vaults
    r = _schist("--vault", str(alpha), "add", "--vault", str(beta), "--title", "t", "--body", "b")
    assert r.returncode == 0, r.stderr
    assert _notes(alpha) == [] and len(_notes(beta)) == 1


def test_the_environment_still_supplies_the_vault(vaults):
    alpha, _ = vaults
    r = _schist("add", "--title", "t", "--body", "b", env_vault=str(alpha))
    assert r.returncode == 0, r.stderr
    assert len(_notes(alpha)) == 1


@pytest.mark.parametrize("argv", [
    ["doctor", "--vault", "{a}"],
    ["search", "--vault", "{a}", "x"],
    ["context", "--vault", "{a}"],
    ["sync", "push", "--vault", "{a}"],
], ids=["doctor", "search", "context", "sync-push"])
def test_documented_forms_parse(vaults, argv):
    """The docs write these; argparse used to exit 2 on each."""
    alpha, _ = vaults
    r = _schist(*[a.format(a=alpha) for a in argv])
    assert "unrecognized arguments" not in r.stderr
    assert r.returncode != 2, r.stderr


def test_db_after_the_subcommand_is_used(vaults, tmp_path):
    alpha, _ = vaults
    db = tmp_path / "elsewhere.db"
    r = _schist("--vault", str(alpha), "context", "--db", str(db))
    assert r.returncode == 0, r.stderr
    assert db.exists()
