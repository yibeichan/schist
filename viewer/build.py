#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""Compatibility entry point for ``python viewer/build.py --db ... --out ...``.

The builder lives in the CLI package as ``schist.viewer.build``; prefer
``schist build`` or ``python -m schist.viewer.build``. This shim loads the
builder from this checkout by file path rather than importing ``schist``, so
an installed ``schist`` of another version, or none at all, does not change
what it runs. The builder is stdlib-only, so no install is needed.
"""

import importlib.util
from pathlib import Path

BUILDER = Path(__file__).resolve().parents[1] / "cli" / "schist" / "viewer" / "build.py"

if __name__ == "__main__":
    spec = importlib.util.spec_from_file_location("schist_viewer_build", BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
