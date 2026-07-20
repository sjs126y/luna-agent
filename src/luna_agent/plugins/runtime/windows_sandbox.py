"""Native Windows AppContainer plugin worker launch boundary.

The actual AppContainer process is created by the Worker client because Windows
requires the inherited stdio handles at process creation time. This module keeps
platform detection explicit and fail-closed on unsupported Python builds.
"""

from __future__ import annotations

import os
from pathlib import Path


def appcontainer_launch(
    *,
    python: Path,
    plugin_root: Path,
    environment_root: Path,
    data_root: Path,
    allow_network: bool,
):
    if os.name != "nt":
        raise RuntimeError("AppContainer is available only on native Windows")
    raise RuntimeError(
        "Native Windows AppContainer worker creation is unavailable in this build; "
        "external plugins remain disabled. Use process-only only for local development."
    )
