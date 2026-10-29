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


def _npm_global_entry() -> Optional[Path]:
    """`dist/index.js` of a global `npm install -g @schist/mcp-server`, or None
    when npm is missing, fails, or has no such package installed."""
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=True,
                              timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not root:
        return None
    return Path(root) / NPM_PACKAGE / "dist" / "index.js"


def find_mcp_server_entry() -> Optional[str]:
    """Absolute path of the MCP server's `dist/index.js`: a source checkout's
    first, since a developer running from one means that build, then the npm
    global install. None when neither exists."""
    for candidate in (_checkout_entry(), _npm_global_entry()):
        if candidate is not None and candidate.is_file():
            return str(candidate.resolve())
    return None
