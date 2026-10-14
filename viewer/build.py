#!/usr/bin/env python3
"""Compatibility entry point. The builder lives in the CLI package as
``schist.viewer.build``; prefer ``schist build`` or
``python -m schist.viewer.build``."""

import sys
from pathlib import Path

try:
    from schist.viewer.build import main
except ImportError:  # source checkout without the CLI installed
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))
    from schist.viewer.build import main

if __name__ == "__main__":
    main()
