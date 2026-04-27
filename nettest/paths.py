"""Centralized path resolution for dev and frozen (PyInstaller) mode.

When the application is packaged as a single binary:
  - sys.frozen  is True
  - sys._MEIPASS is the temp dir where PyInstaller extracted bundled data
  - sys.executable is the binary path

External directories (ovf/, artifacts/, nettest.config.json) sit beside the
binary and are accessed via get_workspace().

Static web assets are bundled inside the binary (extracted to _MEIPASS/static)
and accessed via get_static_dir().
"""

from __future__ import annotations

import sys
from pathlib import Path


def get_workspace() -> Path:
    """Return the directory that holds nettest.config.json, ovf/, and artifacts/.

    - Frozen : directory of the binary (sys.executable)
    - Dev    : repository root (two levels above this file)
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_static_dir() -> Path:
    """Return the directory for web UI static assets.

    - Frozen : sys._MEIPASS/static  (extracted from the binary)
    - Dev    : <workspace>/static
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "static"  # type: ignore[attr-defined]
    return get_workspace() / "static"
