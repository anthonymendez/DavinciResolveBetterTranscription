"""Install-layout paths shared by the engine (see docs/ARCHITECTURE.md, "Install layout")."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "BetterCaptions"


def data_dir() -> Path:
    """``$BETTER_CAPTIONS_HOME`` if set, else the platform's per-user data dir + ``BetterCaptions``."""
    override = os.environ.get("BETTER_CAPTIONS_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME


def models_dir() -> Path:
    """Where downloaded model files are cached."""
    return data_dir() / "models"
