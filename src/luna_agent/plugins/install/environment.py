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
from typing import Any, Iterable, Mapping
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


class PluginEnvironmentLease:
    def __init__(self, path: Path, handle) -> None:
        self.path = path
        self.handle = handle
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        _unlock_file(self.handle)
        self.handle.close()
        try:
            self.path.unlink()
            self.path.parent.rmdir()
        except OSError:
            pass


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

    def acquire_lease(
        self,
        plugin_key: str,
        environment_id: str,
        runtime_instance_id: str,
    ) -> PluginEnvironmentLease:
        lease_root = self.path_for(plugin_key, environment_id) / ".leases"
        lease_root.mkdir(parents=True, exist_ok=True)
        safe_runtime = hashlib.sha256(runtime_instance_id.encode("utf-8")).hexdigest()[:24]
        path = lease_root / f"{os.getpid()}-{safe_runtime}.lock"
        handle = path.open("a+b")
        if path.stat().st_size == 0:
            handle.write(b"1")
            handle.flush()
        _lock_file(handle, blocking=True)
        return PluginEnvironmentLease(path, handle)

    def collect_garbage(
        self,
        *,
        retained: Mapping[tuple[str, str], Iterable[str]],
        retain_plugin_keys: Iterable[str] = (),
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Report or remove environments with no package, runtime, or lease reference."""
        retained_reasons = {
            (str(key), str(environment_id)): sorted({str(reason) for reason in reasons})
            for (key, environment_id), reasons in retained.items()
        }
        conservative_keys = {str(key) for key in retain_plugin_keys}
        kept: list[dict[str, Any]] = []
        removable: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return {
                "dry_run": bool(dry_run),
                "retained": kept,
                "removable": removable,
                "removed": removed,
                "bytes_reclaimable": 0,
            }
        root = self.root.resolve()
        for plugin_dir in sorted(self.root.iterdir()):
            if plugin_dir.name == ".staging" or not plugin_dir.is_dir() or plugin_dir.is_symlink():
                continue
            for environment_dir in sorted(plugin_dir.iterdir()):
                if not environment_dir.is_dir() or environment_dir.is_symlink():
                    continue
                resolved = environment_dir.resolve()
                if root not in resolved.parents:
                    continue
                metadata = _read_environment_metadata(environment_dir)
                plugin_key = str(metadata.get("plugin_key") or "")
                environment_id = str(metadata.get("environment_id") or environment_dir.name)
                size_bytes = _directory_size(environment_dir)
                item = {
                    "plugin_key": plugin_key,
                    "environment_id": environment_id,
                    "path": str(environment_dir),
                    "size_bytes": size_bytes,
                }
                reasons = retained_reasons.get((plugin_key, environment_id), [])
                if _has_active_lease(environment_dir):
                    reasons = sorted({*reasons, "active_process_lease"})
                if not plugin_key:
                    reasons = ["invalid_metadata"]
                elif plugin_key in conservative_keys:
                    reasons = ["installed_manifest_unavailable"]
                if reasons:
                    kept.append({**item, "reasons": reasons})
                    continue
                removable.append(item)
                if not dry_run:
                    shutil.rmtree(environment_dir)
                    removed.append(item)
        return {
            "dry_run": bool(dry_run),
            "retained": kept,
            "removable": removable,
            "removed": removed,
            "bytes_reclaimable": sum(item["size_bytes"] for item in removable),
        }

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


def _read_environment_metadata(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / "environment.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _directory_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _lock_file(handle, *, blocking: bool) -> bool:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(handle.fileno(), mode, 1)
        except OSError:
            return False
        return True
    import fcntl

    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        return False
    return True


def _unlock_file(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _has_active_lease(environment_root: Path) -> bool:
    lease_root = environment_root / ".leases"
    if not lease_root.is_dir():
        return False
    active = False
    for path in lease_root.glob("*.lock"):
        try:
            handle = path.open("r+b")
        except OSError:
            active = True
            continue
        try:
            if _lock_file(handle, blocking=False):
                _unlock_file(handle)
                try:
                    path.unlink()
                except OSError:
                    pass
            else:
                active = True
        finally:
            handle.close()
    try:
        lease_root.rmdir()
    except OSError:
        pass
    return active
