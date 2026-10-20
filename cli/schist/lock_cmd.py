# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""`schist lock hold`: hold the vault write lock for another process (#724).

The MCP server cannot flock from Node, so it spawns this helper and keeps its
stdin open for the span of one write. Closing stdin (or the parent dying)
releases the lock; so does --max-hold, a valve against a hung parent.
"""
import select
import sys
import time

from . import git_ops

EXIT_TIMEOUT = 75
EXIT_EXPIRED = 76


def hold(vault_path: str, timeout: float, max_hold: float) -> int:
    try:
        with git_ops.vault_write_lock(vault_path, timeout=timeout):
            print("LOCKED", flush=True)
            deadline = time.monotonic() + max_hold
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    print("EXPIRED", flush=True)
                    return EXIT_EXPIRED
                ready, _, _ = select.select([sys.stdin], [], [], min(left, 1.0))
                if ready and not sys.stdin.buffer.read1(4096):
                    return 0
    except git_ops.VaultLockTimeout as e:
        print(f"TIMEOUT holder={e.holder}", flush=True)
        return EXIT_TIMEOUT
