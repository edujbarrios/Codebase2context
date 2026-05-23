#!/usr/bin/env python3
"""
Thin wrapper that lets this example folder behave like a standalone repo that
contains `codebase2context.py`, matching the primary usage pattern.

It executes the real tool from the parent directory.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> None:
    tool = (Path(__file__).resolve().parent.parent / "codebase2context.py").resolve()
    sys.argv[0] = str(tool)
    runpy.run_path(str(tool), run_name="__main__")


if __name__ == "__main__":
    main()

