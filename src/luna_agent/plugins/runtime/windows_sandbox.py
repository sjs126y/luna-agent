"""Plugin-specific adapter for the shared Windows AppContainer launcher."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Sequence

from luna_agent.security.windows_appcontainer import (
    AppContainerLease,
    AppContainerProcess,
    _configure_winapi,
    cleanup_appcontainer_profile,
)

# Compatibility aliases for integrations that imported the old private names.
_AppContainerProfileLease = AppContainerLease
_AppContainerProcess = AppContainerProcess
_remove_appcontainer_profile = cleanup_appcontainer_profile


def appcontainer_launch(
    *,
    python: Path,
    plugin_root: Path,
    environment_root: Path,
    data_root: Path,
    allow_network: bool,
    plugin_key: str,
    runtime_instance_id: str,
):
    """Build one isolated external plugin Worker generation."""
    if os.name != "nt":
        raise RuntimeError("AppContainer is available only on native Windows")

    from luna_agent.plugins.runtime.sandbox import PluginWorkerLaunch

    plugin_root = plugin_root.resolve()
    environment_root = environment_root.resolve()
    data_root = data_root.resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    runtime_roots = _python_runtime_roots(python, environment_root)
    base_python = runtime_roots[0] / "python.exe"
    if not base_python.is_file():
        raise RuntimeError(f"Windows base Python executable does not exist: {base_python}")

    profile = AppContainerLease(
        profile_name=_profile_name(plugin_key, runtime_instance_id),
        roots=(plugin_root, environment_root, *runtime_roots, data_root),
        active_process_limit=1,
        lease_root=data_root / ".appcontainer-leases",
    )

    def process_factory(command: Sequence[str], cwd: Path, env: dict[str, str]):
        worker_command = (str(base_python), *tuple(command)[1:])
        worker_env = {
            **env,
            "__PYVENV_LAUNCHER__": str(Path(python).resolve()),
        }
        return profile.spawn(
            command=worker_command,
            cwd=cwd,
            env=worker_env,
            readable_roots=(plugin_root, environment_root, *runtime_roots),
            writable_roots=(data_root,),
            allow_network=allow_network,
        )

    return PluginWorkerLaunch(
        argv=(str(base_python), "-m", "luna_agent_plugin_sdk.worker"),
        cwd=data_root,
        backend="appcontainer",
        filesystem_isolated=True,
        network_isolated=not allow_network,
        process_factory=process_factory,
        cleanup=profile.close,
    )


def _profile_name(plugin_key: str, runtime_instance_id: str = "") -> str:
    identity = f"{plugin_key}\0{runtime_instance_id or 'legacy'}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"LunaAgent.Plugin.{digest}"


def _python_runtime_roots(python: Path, environment_root: Path) -> tuple[Path, ...]:
    """Resolve the base runtime required by a Windows venv launcher."""
    config = Path(environment_root) / "pyvenv.cfg"
    home = ""
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip().lower() == "home":
            home = value.strip().strip('"')
            break
    candidate = Path(home) if home else Path(python).resolve().parent
    if not candidate.is_absolute():
        raise RuntimeError(f"Windows plugin Python home is not absolute: {candidate}")
    candidate = candidate.resolve()
    if candidate == Path(candidate.anchor):
        raise RuntimeError(f"Windows plugin Python home is too broad: {candidate}")
    if not candidate.is_dir():
        raise RuntimeError(f"Windows plugin Python home does not exist: {candidate}")
    return (candidate,)

