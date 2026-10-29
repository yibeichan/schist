# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""Find the MCP server's entry point (`dist/index.js`) on this machine.

`schist init --print-mcp-config` and `schist doctor` both need it, and both
looked only beside a source checkout (`<repo>/mcp-server/dist/index.js`). A
`pip install schist` has no checkout, so after the README's
`pip install schist` and `npm install -g @schist/mcp-server`, the first
`--print-mcp-config` failed. The npm global install is the other place it
can be.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

NPM_PACKAGE = "@schist/mcp-server"


def _checkout_entry() -> Path:
    """Where a source checkout keeps the built server, beside cli/."""
    return Path(__file__).resolve().parents[2] / "mcp-server" / "dist" / "index.js"


def _run_stdout(cmd: list[str]) -> str:
    """A command's stripped stdout, or "" when it is missing, fails or hangs."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _npm_global_entry() -> Optional[Path]:
    """`dist/index.js` of a global `npm install -g @schist/mcp-server`, or None
    when there is no npm global root to look in.

    The root is `npm root -g`. npm prints `***` in place of a path segment that
    looks like a secret (a dashed UUID), so for such a root it is worked out
    from node's own path instead: `<prefix>/bin/node` keeps global packages in
    `<prefix>/lib/node_modules`."""
    root = _run_stdout(["npm", "root", "-g"])
    if not root or "***" in root:
        node = _run_stdout(["node", "-p", "process.execPath"])
        if not node:
            return None
        root = str(Path(node).parent.parent / "lib" / "node_modules")
    return Path(root) / NPM_PACKAGE / "dist" / "index.js"


def find_mcp_server_entry() -> Optional[str]:
    """Absolute path of the MCP server's `dist/index.js`: a source checkout's
    first, since a developer running from one means that build, then the npm
    global install. None when neither exists."""
    checkout = _checkout_entry()
    if checkout.is_file():
        return str(checkout.resolve())
    npm = _npm_global_entry()          # only now: `npm root -g` costs a process
    if npm is not None and npm.is_file():
        return str(npm.resolve())
    return None
