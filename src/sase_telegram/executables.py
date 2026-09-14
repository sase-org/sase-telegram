"""Helpers for launching installed SASE console scripts."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def resolve_console_script(script_name: str) -> str:
    """Return the best executable path for an installed console script."""
    sibling = Path(sys.executable).with_name(script_name)
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    resolved = shutil.which(script_name)
    if resolved:
        return resolved
    return script_name


__all__ = ["resolve_console_script"]
