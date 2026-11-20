# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""Find and remove atomic-write temp files orphaned in `<vault>/.schist/tmp/`.

A note write stages its content in a temp file under `.schist/tmp/` and renames
it over the note. A hard kill (SIGKILL, OOM, power loss) between the write and
the rename leaves the temp behind (#433, #448). The directory is gitignored and
never a sync scope, so an orphan is inert, but nothing else removes it.

Two writers stage there and name their temps differently:

* CLI `markdown_io._atomic_write`: `tempfile.mkstemp(suffix='.tmp')`, giving
  `tmp` + 8 characters from `[a-z0-9_]` + `.tmp`. The name holds no pid.
* MCP server `git-writer.atomicWriteFile`:
  `.<basename>.<pid>.<12 lowercase hex>.tmp`.

Both formats are matched here, anchored and ASCII-only, so the listing and
removal cover the whole directory even though each writer only creates its own
format. Nothing else is matched: other files and directories under
`.schist/tmp/` are never touched, and symlinks are never followed.

A temp is stale when it is at least `ANY_AGE` seconds old, whatever its pid
says. A vault shared over NFS can be written from another node, where a pid
that looks dead on this host belongs to a live writer, so age alone is the
default rule.

`trust_pid=True` adds a shortcut for single-host vaults only: a temp whose
owning pid (MCP format only) is gone is also stale once it is
`DEAD_PID_MIN_AGE` seconds old. Never pass it for a vault that another host
can write.

Every per-file error is swallowed: reaping must never stop a write or a
startup. `find_stale_temps` raises OSError only when the directory itself
cannot be listed (a missing directory gives an empty list), so a caller that
reports can tell "nothing there" from "could not look"; `reap_stale_temps`
never raises.
"""

from __future__ import annotations

import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

DEAD_PID_MIN_AGE = 300.0
ANY_AGE = 3600.0

_CLI_TEMP = re.compile(r"tmp[a-z0-9_]{8}\.tmp", re.ASCII)
_MCP_TEMP = re.compile(
    r"\.[A-Za-z0-9._-]{1,200}\.([0-9]{1,10})\.[0-9a-f]{12}\.tmp", re.ASCII
)


@dataclass(frozen=True)
class StaleTemp:
    path: Path
    age: float
    pid: Optional[int]


def staging_dir(vault_root: str | os.PathLike) -> Path:
    return Path(vault_root) / ".schist" / "tmp"


def _owner_pid(name: str) -> tuple[bool, Optional[int]]:
    """Return (is_ours, pid). pid is None for the CLI format, which has none."""
    if _CLI_TEMP.fullmatch(name):
        return True, None
    m = _MCP_TEMP.fullmatch(name)
    if m:
        return True, int(m.group(1))
    return False, None


def _pid_alive(pid: int) -> bool:
    """True unless `pid` is known not to exist on this host."""
    if pid <= 0:
        return True  # kill(0, ...) would signal our own process group
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError, OSError):
        return True
    return True


def find_stale_temps(
    vault_root: str | os.PathLike,
    *,
    now: Optional[float] = None,
    trust_pid: bool = False,
    pid_alive: Callable[[int], bool] = _pid_alive,
) -> list[StaleTemp]:
    """List stale atomic-write temps under `<vault_root>/.schist/tmp/`.

    `trust_pid`: single-host vaults only, see the module docstring.
    """
    if now is None:
        now = time.time()
    root = staging_dir(vault_root)
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return []
    found: list[StaleTemp] = []
    for name in sorted(names):
        try:
            ours, pid = _owner_pid(name)
            if not ours:
                continue
            path = root / name
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                continue  # symlinks and directories are never touched
            age = now - st.st_mtime
            if age < ANY_AGE and not (
                trust_pid
                and pid is not None
                and age >= DEAD_PID_MIN_AGE
                and not pid_alive(pid)
            ):
                continue  # also skips a future mtime
            found.append(StaleTemp(path, age, pid))
        except Exception:
            continue
    return found


def reap_stale_temps(
    vault_root: str | os.PathLike,
    *,
    now: Optional[float] = None,
    trust_pid: bool = False,
    pid_alive: Callable[[int], bool] = _pid_alive,
) -> list[Path]:
    """Unlink the stale temps and return the paths removed. Never raises."""
    removed: list[Path] = []
    try:
        stale = find_stale_temps(
            vault_root, now=now, trust_pid=trust_pid, pid_alive=pid_alive
        )
    except Exception:
        return removed
    for item in stale:
        try:
            os.unlink(item.path)
            removed.append(item.path)
        except Exception:
            continue
    return removed
