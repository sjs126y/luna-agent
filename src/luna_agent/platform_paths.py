"""Platform-aware defaults for user-owned runtime data."""

from __future__ import annotations

import os
from pathlib import Path


def default_data_dir() -> Path:
    """Return the default runtime data directory without moving explicit config."""
    if os.name == "nt":
        local_appdata = str(os.environ.get("LOCALAPPDATA") or "").strip()
        if local_appdata:
            return Path(local_appdata) / "LunaAgent"
    return Path("./data")
