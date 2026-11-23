# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""Report old atomic-write files. Cleanup requires a quiescent vault."""

from __future__ import annotations

import os
import re
import stat
import time
from contextlib import ExitStack
from pathlib import Path

ANY_AGE = 3600
_CLI_TEMP = re.compile(r"tmp[a-z0-9_]{8}\.tmp", re.ASCII)
_MCP_TEMP = re.compile(r"\..+\.[0-9]+\.[0-9a-f]{12}\.tmp", re.DOTALL)


def _scan(vault_root, *, now=None, remove=False) -> list[Path]:
    root = Path(vault_root)
    cutoff = (time.time() if now is None else now) - ANY_AGE
    with ExitStack() as stack:
        flags = os.O_RDONLY | os.O_DIRECTORY
        fd = os.open(root, flags)
        stack.callback(os.close, fd)
        for part in (".schist", "tmp"):
            try:
                fd = os.open(part, flags | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return []
            stack.callback(os.close, fd)
        result = []
        for name in sorted(os.listdir(fd)):
            if not (_CLI_TEMP.fullmatch(name) or _MCP_TEMP.fullmatch(name)):
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_mtime > cutoff:
                    continue
                if remove:
                    os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                continue
            result.append(root / ".schist" / "tmp" / name)
        return result


def find_stale_temps(vault_root, *, now=None) -> list[Path]:
    return _scan(vault_root, now=now)


def cleanup_temps(vault_root, *, writers_stopped: bool) -> list[Path]:
    if not writers_stopped:
        raise ValueError("stop all vault writers on every host before removing old temporary files")
    return _scan(vault_root, remove=True)
