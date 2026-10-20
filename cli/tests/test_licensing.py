# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""The license boundary: every code file names exactly one license in its own
SPDX header, and the AGPL files are exactly the documented ones.

`reuse lint` alone cannot catch a header-less file (REUSE.toml's catch-all
covers it, so a new hub module would silently become Apache-2.0) or a file
with two license lines (both are reported, neither is an error)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Built from parts so this file does not itself declare these as its licenses.
TAG = "SPDX-" + "License-Identifier"
# The whole expression, so `AGPL-3.0-or-later OR Apache-2.0` is not read as AGPL.
TAG_RE = re.compile(rf"^\W*{TAG}:\s*(.*?)\s*(?:-->|\*/)?\s*$", re.MULTILINE)

AGPL = "AGPL-3.0-or-later"
APACHE = "Apache-2.0"

CODE_SUFFIXES = {".py", ".ts", ".js", ".mjs", ".cjs", ".sh", ".html"}
AGPL_DIRS = ("mcp-server/", "rag/")
AGPL_FILES = {
    "hooks/pre-receive",
    "cli/schist/hub_admin.py",
    "cli/schist/hub_keys.py",
    "cli/schist/hub_shell.py",
    "cli/schist/pre_receive.py",
    "cli/schist/viewer/agent.py",
    "cli/schist/viewer/proposals.py",
    "cli/schist/viewer/serve.py",
}
# Covered by REUSE.toml instead of a header: the hooks must match sync.py's
# constants byte for byte, and .claude/skills is third-party (MIT).
HEADERLESS_FILES = {"hooks/post-commit", "hooks/pre-commit"}
HEADERLESS_DIRS = (".claude/skills/",)


def _tracked() -> list[str]:
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("needs a git checkout of the repository")
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT,
                         capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def _code_files() -> list[str]:
    files = []
    for path in _tracked():
        if path in HEADERLESS_FILES or path.startswith(HEADERLESS_DIRS):
            continue
        if Path(path).suffix in CODE_SUFFIXES or path == "hooks/pre-receive":
            files.append(path)
    return files


def _expected(path: str) -> str:
    return AGPL if path in AGPL_FILES or path.startswith(AGPL_DIRS) else APACHE


def test_every_code_file_names_exactly_its_components_license():
    wrong = []
    for path in _code_files():
        head = (REPO_ROOT / path).read_text(encoding="utf-8").splitlines()[:10]
        found = TAG_RE.findall("\n".join(head))
        if found != [_expected(path)]:
            wrong.append(f"{path}: header names {found or 'no license'}, expected {_expected(path)}")
    assert not wrong, "\n".join(wrong)


def test_documented_agpl_files_exist():
    tracked = set(_tracked())
    assert AGPL_FILES <= tracked, sorted(AGPL_FILES - tracked)
