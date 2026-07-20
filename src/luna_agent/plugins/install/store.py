"""Atomic operational state for installed plugin packages."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from luna_agent.persistence.json_store import read_json_object, write_json_atomic


class PluginInstallStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._state = self._load()

    @property
    def revision(self) -> int:
        return int(self._state.get("revision") or 0)

    def packages(self) -> dict[str, Any]:
        return deepcopy(self._state.get("packages", {}))

    def active_path(self, plugin_key: str) -> Path | None:
        record = self._state.get("packages", {}).get(plugin_key, {})
        digest = str(record.get("active_package") or "")
        version = record.get("versions", {}).get(digest, {})
        path = str(version.get("path") or "")
        return Path(path) if path else None

    def active_paths(self) -> list[Path]:
        result = []
        for key in sorted(self._state.get("packages", {})):
            path = self.active_path(key)
            if path is not None:
                result.append(path)
        return result

    def enabled_for(self, plugin_key: str) -> bool | None:
        record = self._state.get("packages", {}).get(plugin_key)
        if not isinstance(record, dict) or "enabled" not in record:
            return None
        return bool(record["enabled"])

    def record_install(
        self,
        *,
        plugin_key: str,
        digest: str,
        path: Path,
        version: str,
        source: str,
        enabled: bool = True,
    ) -> None:
        packages = self._state.setdefault("packages", {})
        record = packages.setdefault(plugin_key, {"versions": {}})
        record.setdefault("versions", {})[digest] = {
            "path": str(Path(path).resolve()),
            "version": version,
            "source": source,
        }
        record["active_package"] = digest
        record["status"] = "active"
        record["enabled"] = bool(enabled)
        self._save()

    def activate(self, plugin_key: str, digest: str) -> Path:
        path = self.package_path(plugin_key, digest)
        record = self._state["packages"][plugin_key]
        record["active_package"] = digest
        record["status"] = "active"
        self._save()
        return path

    def package_path(self, plugin_key: str, digest: str) -> Path:
        record = self._state.get("packages", {}).get(plugin_key)
        if not isinstance(record, dict) or digest not in record.get("versions", {}):
            raise KeyError(f"Installed plugin package not found: {plugin_key}@{digest}")
        return Path(record["versions"][digest]["path"])

    def mark_pending_removal(self, plugin_key: str) -> None:
        record = self._state.get("packages", {}).get(plugin_key)
        if isinstance(record, dict):
            record["status"] = "pending_removal"
            self._save()

    def set_enabled(self, plugin_key: str, enabled: bool) -> None:
        record = self._state.get("packages", {}).get(plugin_key)
        if isinstance(record, dict):
            record["enabled"] = bool(enabled)
            self._save()

    def repair_paths(self, packages_root: Path) -> int:
        """Rebase stale absolute paths after a repository/data-root move."""
        repaired = 0
        root = Path(packages_root)
        for plugin_key, record in self._state.get("packages", {}).items():
            versions = record.get("versions", {}) if isinstance(record, dict) else {}
            for digest, item in versions.items():
                if not isinstance(item, dict):
                    continue
                current = Path(str(item.get("path") or ""))
                candidate = root / plugin_key.replace("/", "__") / str(digest)
                if current.exists() or not candidate.is_dir():
                    continue
                item["path"] = str(candidate.resolve())
                repaired += 1
        if repaired:
            self._save()
        return repaired

    def remove(self, plugin_key: str) -> dict[str, Any]:
        record = self._state.setdefault("packages", {}).pop(plugin_key, {})
        self._save()
        return deepcopy(record)

    def _load(self) -> dict[str, Any]:
        data = read_json_object(
            self.path,
            {"schema_version": 1, "revision": 0, "packages": {}},
        )
        if int(data.get("schema_version") or 0) != 1:
            raise ValueError("Unsupported plugin install state schema")
        packages = data.get("packages", {})
        if not isinstance(packages, dict):
            raise ValueError("Plugin install state packages must be an object")
        return {
            "schema_version": 1,
            "revision": int(data.get("revision") or 0),
            "packages": packages,
        }

    def _save(self) -> None:
        self._state["revision"] = self.revision + 1
        write_json_atomic(self.path, self._state)
