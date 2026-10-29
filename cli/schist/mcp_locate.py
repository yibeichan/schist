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

import fnmatch
import os
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
    looks like a secret (a dashed UUID), so such a root names nothing. node's
    own prefix (`<prefix>/bin/node` keeps globals in `<prefix>/lib/node_modules`)
    is used instead only when it matches what npm printed, segment by segment
    with `***` matching the hidden part; when it does not (Homebrew, a
    distribution's npm, NPM_CONFIG_PREFIX set elsewhere) the root is unknown,
    rather than guessed."""
    root = _run_stdout(["npm", "root", "-g"])
    if not root:
        return None
    if "***" in root:
        # A prefix set in the environment is where npm installs; take it as
        # given rather than guess from node, whose prefix it can differ from
        # only in the hidden part.
        configured = os.environ.get("NPM_CONFIG_PREFIX") or os.environ.get("npm_config_prefix")
        if configured:
            return Path(configured) / "lib" / "node_modules" / NPM_PACKAGE / "dist" / "index.js"
        node = _run_stdout(["node", "-p", "process.execPath"])
        if not node:
            return None
        guess = Path(node).parent.parent / "lib" / "node_modules"
        printed = Path(root).parts
        if len(printed) != len(guess.parts) or not all(
                fnmatch.fnmatchcase(part, shown.replace("***", "*"))
                for part, shown in zip(guess.parts, printed)):
            return None
        root = str(guess)
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
