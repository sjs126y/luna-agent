"""Content-addressed Python environments for external plugins."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from luna_agent_plugin_sdk import SDK_VERSION

from luna_agent.persistence.json_store import write_json_atomic


@dataclass(frozen=True, slots=True)
class PluginEnvironment:
    plugin_key: str
    environment_id: str
    root: Path
    python: Path
    dependencies: tuple[str, ...]
    status: str = "ready"

    def as_dict(self) -> dict[str, object]:
        return {
            "plugin_key": self.plugin_key,
            "environment_id": self.environment_id,
            "root": str(self.root),
            "python": str(self.python),
            "dependencies": list(self.dependencies),
            "status": self.status,
        }


class PluginEnvironmentManager:
    """Build immutable per-plugin virtual environments without touching the host venv."""

    def __init__(
        self,
        root: Path,
        *,
        sdk_source: Path | None = None,
        uv_command: str = "uv",
    ) -> None:
        self.root = Path(root)
        self.staging_root = self.root / ".staging"
        self.sdk_source = Path(sdk_source).resolve() if sdk_source else _default_sdk_source()
        self.uv_command = uv_command

    def environment_id(self, plugin_key: str, dependencies: Iterable[str]) -> str:
        normalized = _normalize(dependencies)
        payload = json.dumps(
            {
                "plugin": plugin_key,
                "python_abi": getattr(sys.implementation, "cache_tag", "python"),
                "platform": platform.system().lower(),
                "machine": platform.machine().lower(),
                "sdk": SDK_VERSION,
                "dependencies": normalized,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def path_for(self, plugin_key: str, environment_id: str) -> Path:
        return self.root / _safe_key(plugin_key) / environment_id

    def inspect(self, plugin_key: str, dependencies: Iterable[str]) -> PluginEnvironment:
        normalized = _normalize(dependencies)
        environment_id = self.environment_id(plugin_key, normalized)
        root = self.path_for(plugin_key, environment_id)
        return PluginEnvironment(
            plugin_key=plugin_key,
            environment_id=environment_id,
            root=root,
            python=_venv_python(root),
            dependencies=normalized,
            status="ready" if _environment_ready(root) else "missing",
        )

    def ensure(self, plugin_key: str, dependencies: Iterable[str]) -> PluginEnvironment:
        expected = self.inspect(plugin_key, dependencies)
        if expected.status == "ready":
            return expected
        self.staging_root.mkdir(parents=True, exist_ok=True)
        transaction = self.staging_root / uuid4().hex
        candidate = transaction / "environment"
        destination = expected.root
        try:
            transaction.mkdir(parents=True)
            self._run([self.uv_command, "venv", "--python", sys.executable, str(candidate)])
            install = [
                self.uv_command,
                "pip",
                "install",
                "--python",
                str(_venv_python(candidate)),
                str(self.sdk_source),
            ]
            self._run(install)
            if expected.dependencies:
                self._run([
                    self.uv_command,
                    "pip",
                    "install",
                    "--python",
                    str(_venv_python(candidate)),
                    "--only-binary",
                    ":all:",
                    *expected.dependencies,
                ])
            self._run([
                str(_venv_python(candidate)),
                "-c",
                "import luna_agent_plugin_sdk; print(luna_agent_plugin_sdk.SDK_VERSION)",
            ])
            write_json_atomic(candidate / "environment.json", {
                **expected.as_dict(),
                "root": str(destination),
                "python": str(_venv_python(destination)),
                "status": "ready",
            })
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                shutil.rmtree(candidate, ignore_errors=True)
            else:
                os.replace(candidate, destination)
        finally:
            shutil.rmtree(transaction, ignore_errors=True)
        ready = self.inspect(plugin_key, expected.dependencies)
        if ready.status != "ready":
            raise RuntimeError(f"Plugin environment was not committed: {plugin_key}")
        return ready

    def remove(self, plugin_key: str, environment_id: str) -> None:
        shutil.rmtree(self.path_for(plugin_key, environment_id), ignore_errors=True)

    @staticmethod
    def _run(argv: list[str]) -> None:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,
            check=False,
        )
        if completed.returncode != 0:
            tail = (completed.stdout or "")[-8000:]
            raise RuntimeError(f"Plugin environment command failed ({completed.returncode}): {tail}")


def _normalize(dependencies: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(
        (str(item).strip() for item in dependencies if str(item).strip()),
        key=str.lower,
    ))


def _environment_ready(root: Path) -> bool:
    return (root / "environment.json").is_file() and _venv_python(root).is_file()


def _venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _safe_key(plugin_key: str) -> str:
    return plugin_key.replace("/", "__")


def _default_sdk_source() -> Path:
    return Path(__file__).resolve().parents[4] / "packages" / "luna-agent-plugin-sdk"
