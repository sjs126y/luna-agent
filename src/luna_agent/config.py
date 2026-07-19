"""Central config facade backed by the registry loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from luna_agent.config_loader import ConfigLoader


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

        self.cron_jobs_path: Path = Path("data/cron")

    def get_env(self, name: str, default: str = "") -> str:
        """Resolve an environment-backed value through the settings boundary."""
        value = self._environment.get(str(name), default)
        return str(value or default)
