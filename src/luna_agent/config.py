"""Central config facade backed by the registry loader."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from luna_agent.config_loader import ConfigLoader
from luna_agent.platform_paths import default_data_dir


class Settings:
    def __init__(self, **overrides: Any) -> None:
        loader = ConfigLoader()
        snapshot = loader.load(overrides=overrides, strict=True)
        self.config_snapshot = snapshot
        self.raw_env: dict[str, str] = snapshot.raw_env
        self.raw_config: dict[str, Any] = snapshot.raw_config
        self._environment: dict[str, str] = snapshot.environment
        for attr, value in snapshot.attr_values.items():
            setattr(self, attr, value)
        if os.name == "nt":
            # Only replace registry defaults.  Explicit YAML, .env, and
            # constructor overrides must remain authoritative on every OS.
            if snapshot.sources.get("storage.data_dir") == "default":
                self.agent_data_dir = default_data_dir()
            if snapshot.sources.get("sandbox.roots") == "default":
                self.sandbox_roots = [Path(self.agent_data_dir)]
            if snapshot.sources.get("sandbox.bash_work_dir") == "default":
                self.bash_work_dir = self.agent_data_dir
            if snapshot.sources.get("plugins.dirs") == "default":
                self.plugins_dirs = [Path("./plugins"), Path(self.agent_data_dir) / "plugins"]

        self.cron_jobs_path: Path = Path(self.agent_data_dir) / "cron"

    def get_env(self, name: str, default: str = "") -> str:
        """Resolve an environment-backed value through the settings boundary."""
        value = self._environment.get(str(name), default)
        return str(value or default)
