"""Sandbox launch policy for external plugin workers."""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from luna_agent.tools.process_sandbox import (
    BASH_STRICT_POLICY,
    build_process_launch,
)


PluginSandboxBackend = Literal["auto", "bwrap", "appcontainer", "process-only"]


@dataclass(frozen=True, slots=True)
class PluginWorkerLaunch:
    argv: tuple[str, ...]
    cwd: Path
    backend: str
    filesystem_isolated: bool
    network_isolated: bool
    warning: str = ""
    process_factory: Any | None = None
    cleanup: Callable[[], None] | None = None


def build_plugin_worker_launch(
    *,
    python: Path,
    plugin_root: Path,
    environment_root: Path,
    data_root: Path,
    allow_network: bool = False,
    backend: PluginSandboxBackend | str = "auto",
    plugin_key: str = "external/plugin",
    runtime_instance_id: str = "",
) -> PluginWorkerLaunch:
    """Build a fail-closed launch for one external plugin generation."""
    requested = str(backend or "auto").strip().lower()
    if requested not in {"auto", "bwrap", "appcontainer", "process-only"}:
        raise ValueError(f"Unsupported plugin sandbox backend: {requested}")
    if os.name == "nt":
        return _windows_launch(
            python=python,
            plugin_root=plugin_root,
            environment_root=environment_root,
            data_root=data_root,
            allow_network=allow_network,
            backend=requested,
            plugin_key=plugin_key,
            runtime_instance_id=runtime_instance_id,
        )
    if not sys.platform.startswith("linux"):
        if requested != "process-only":
            raise RuntimeError(
                "External plugins require an OS sandbox on this platform; "
                "set plugins.runtime.sandbox_backend=process-only only for development"
            )
        return _process_only_launch(python=python, data_root=data_root)
    if requested == "appcontainer":
        raise RuntimeError("AppContainer is available only on native Windows")
    if requested == "process-only":
        return _process_only_launch(python=python, data_root=data_root)

    data_root = data_root.resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    command = f"{shlex.quote(str(python.absolute()))} -m luna_agent_plugin_sdk.worker"
    launch = build_process_launch(
        command,
        cwd=data_root,
        writable_roots=(data_root,),
        readable_roots=(plugin_root.resolve(), environment_root.resolve()),
        allow_network=allow_network,
        requested_backend="bwrap" if requested == "bwrap" else "auto",
        policy=BASH_STRICT_POLICY,
    )
    if launch.backend != "bwrap" or not launch.filesystem_isolated:
        raise RuntimeError(launch.warning or "Bubblewrap plugin sandbox is unavailable")
    if not allow_network and not launch.network_isolated:
        raise RuntimeError(
            "Bubblewrap network isolation is unavailable; refusing to start external plugin"
        )
    return PluginWorkerLaunch(
        argv=launch.argv,
        cwd=launch.cwd,
        backend=launch.backend,
        filesystem_isolated=launch.filesystem_isolated,
        network_isolated=launch.network_isolated,
        warning=launch.warning,
    )


def _windows_launch(
    *,
    python: Path,
    plugin_root: Path,
    environment_root: Path,
    data_root: Path,
    allow_network: bool,
    backend: str,
    plugin_key: str,
    runtime_instance_id: str,
) -> PluginWorkerLaunch:
    if backend == "process-only":
        return _process_only_launch(python=python, data_root=data_root)
    if backend not in {"auto", "appcontainer"}:
        raise RuntimeError(f"Plugin sandbox backend is unavailable on Windows: {backend}")
    # AppContainer requires creating the security token and process after the
    # stdio handles exist. The Windows launcher owns that operation.
    from luna_agent.plugins.runtime.windows_sandbox import appcontainer_launch

    return appcontainer_launch(
        python=python,
        plugin_root=plugin_root,
        environment_root=environment_root,
        data_root=data_root,
        allow_network=allow_network,
        plugin_key=plugin_key,
        runtime_instance_id=runtime_instance_id,
    )


def _process_only_launch(*, python: Path, data_root: Path) -> PluginWorkerLaunch:
    data_root = data_root.resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    return PluginWorkerLaunch(
        argv=(str(python.absolute()), "-m", "luna_agent_plugin_sdk.worker"),
        cwd=data_root,
        backend="process-only",
        filesystem_isolated=False,
        network_isolated=False,
        warning="process-only mode provides lifecycle isolation, not an OS security boundary",
    )
