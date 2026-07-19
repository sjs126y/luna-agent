"""Revisioned plugin data used for conservative active-generation cutovers."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


class PluginDataRevisionStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def prepare(self, plugin, *, candidate: bool) -> Path:
        plugin_root = self._plugin_root(plugin.key)
        revisions = plugin_root / "revisions"
        revisions.mkdir(parents=True, exist_ok=True)
        current_id = self.current_revision(plugin.key)
        revision_id = _safe_revision(plugin.runtime_instance_id)
        destination = revisions / revision_id
        if destination.exists():
            shutil.rmtree(destination)
        source = revisions / current_id if current_id else None
        if source is not None and source.is_dir():
            shutil.copytree(source, destination)
        else:
            destination.mkdir(parents=True)
            self._migrate_legacy_files(plugin_root, destination)
        plugin.data_revision_id = revision_id
        plugin.data_path = destination
        if not candidate:
            self.commit(plugin)
        return destination

    def commit(self, plugin) -> None:
        path = Path(plugin.data_path or "")
        expected = self._plugin_root(plugin.key) / "revisions" / plugin.data_revision_id
        if not plugin.data_revision_id or path.resolve() != expected.resolve() or not path.is_dir():
            raise RuntimeError(f"plugin data revision is invalid: {plugin.key}")
        pointer = self._plugin_root(plugin.key) / "current.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        temporary = pointer.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"revision": plugin.data_revision_id}, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, pointer)

    def discard(self, plugin) -> None:
        revision_id = str(getattr(plugin, "data_revision_id", "") or "")
        if not revision_id or revision_id == self.current_revision(plugin.key):
            return
        path = self._plugin_root(plugin.key) / "revisions" / revision_id
        shutil.rmtree(path, ignore_errors=True)

    def current_revision(self, plugin_key: str) -> str:
        pointer = self._plugin_root(plugin_key) / "current.json"
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return ""
        return str(data.get("revision") or "").strip() if isinstance(data, dict) else ""

    def _plugin_root(self, plugin_key: str) -> Path:
        return self.root / plugin_key.replace("/", "__")

    @staticmethod
    def _migrate_legacy_files(plugin_root: Path, destination: Path) -> None:
        if not plugin_root.is_dir():
            return
        for child in plugin_root.iterdir():
            if child.name in {"revisions", "current.json", "current.tmp"}:
                continue
            target = destination / child.name
            if child.is_dir():
                shutil.copytree(child, target)
            elif child.is_file():
                shutil.copy2(child, target)


def _safe_revision(runtime_instance_id: str) -> str:
    value = str(runtime_instance_id or "").rpartition(":")[2]
    cleaned = "".join(char for char in value if char.isalnum() or char in {"-", "_"})
    if not cleaned:
        raise ValueError("plugin runtime instance ID cannot produce a data revision")
    return cleaned
