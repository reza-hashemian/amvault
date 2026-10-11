"""Where the tool keeps its own files: history and known SSH host keys.

Never credentials -- those stay in the vault.
"""

from __future__ import annotations

import os
import sys


def data_dir() -> str:
    override = os.environ.get("NETTOOL_DATA_DIR")
    if override:
        path = override
    elif sys.platform == "win32":
        path = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "nettool")
    elif sys.platform == "darwin":
        path = os.path.expanduser("~/Library/Application Support/nettool")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        path = os.path.join(base, "nettool")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path
