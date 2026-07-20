"""Native Windows AppContainer smoke gate for the external plugin Worker."""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time

from luna_agent.config import Settings
from luna_agent.plugins import PluginManager, PluginStatus
from luna_agent.plugins.runtime import CapabilityKind
from luna_agent.plugins.runtime.windows_sandbox import _configure_winapi, _profile_name


PLUGIN_KEY = "smoke/windows-appcontainer"


def main() -> int:
    if os.name != "nt":
        print("This smoke gate must run under native Windows Python.", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO)

    with tempfile.TemporaryDirectory(prefix="luna-appcontainer-smoke-") as raw_root:
        root = Path(raw_root)
        plugin_root = root / "plugins" / "probe"
        plugin_root.mkdir(parents=True)
        secret = root / "host-secret.txt"
        outside_write = root / "host-write.txt"
        secret.write_text("host-only", encoding="utf-8")
        _write_plugin(plugin_root)
        settings = Settings(
            agent_data_dir=root / "data",
            plugins_dirs=[plugin_root.parent],
            plugins_enabled=[PLUGIN_KEY],
            plugin_worker_isolation=True,
            plugin_sandbox_backend="appcontainer",
            plugin_worker_allow_network=False,
            plugins_config={
                PLUGIN_KEY: {
                    "secret_path": str(secret),
                    "outside_write_path": str(outside_write),
                }
            },
        )
        manager = PluginManager(
            settings,
            plugin_dirs=[plugin_root.parent],
            state_path=root / "state.json",
            include_builtin=False,
        )
        worker = None
        profile_name = ""
        try:
            manager.discover()
            plugin = manager.load_plugin(PLUGIN_KEY)
            if plugin.status is not PluginStatus.LOADED:
                raise RuntimeError(plugin.error or "AppContainer plugin failed to load")
            if plugin.sandbox_backend != "appcontainer":
                raise RuntimeError(
                    f"unexpected sandbox backend: {plugin.sandbox_backend!r}"
                )
            worker = plugin.worker
            if worker is None or not worker.running:
                raise RuntimeError("AppContainer Worker is not running")
            profile_name = _profile_name(PLUGIN_KEY, plugin.runtime_instance_id)
            if _profile_removed(profile_name):
                raise RuntimeError("AppContainer profile mapping was not created")
            route = manager.capability_store.current.view().resolve(
                CapabilityKind.TOOL,
                "windows_appcontainer_probe",
            )
            if route is None:
                raise RuntimeError("probe capability was not published")
            entry = manager.capability_payload(route.binding_id)
            result = asyncio.run(entry.handler())
            expected = {
                "data_write_ok": True,
                "outside_read_blocked": True,
                "outside_write_blocked": True,
                "network_blocked": True,
                "child_process_blocked": True,
            }
            failures = {
                name: result.get(name)
                for name, value in expected.items()
                if result.get(name) is not value
            }
            expected_errors = {
                "read": "PermissionError",
                "write": "PermissionError",
                "network": "PermissionError",
                "child": "OSError",
            }
            failures.update({
                f"{name}_error": result.get("errors", {}).get(name)
                for name, value in expected_errors.items()
                if result.get("errors", {}).get(name) != value
            })
            if failures:
                raise RuntimeError(f"AppContainer boundary probe failed: {failures}")

            manager.external_runtime.worker_supervisor._stopping.add(
                plugin.runtime_instance_id
            )
            native_process = worker.process
            if native_process is None or not getattr(native_process, "_job", None):
                raise RuntimeError("AppContainer Job Object handle is unavailable")
            if not ctypes.windll.kernel32.CloseHandle(native_process._job):
                raise ctypes.WinError()
            native_process._job = wintypes.HANDLE()
            deadline = time.monotonic() + 5.0
            while worker.running and time.monotonic() < deadline:
                time.sleep(0.05)
            if worker.running:
                raise RuntimeError("closing the Job Object did not terminate the Worker")

            manager.close()
            if not _profile_removed(profile_name):
                raise RuntimeError("AppContainer profile was not removed during cleanup")
            print(json.dumps({
                "ok": True,
                "backend": plugin.sandbox_backend,
                "worker_pid": worker.pid,
                "profile_removed": True,
                "probe": result,
            }, ensure_ascii=True, sort_keys=True))
            return 0
        finally:
            try:
                manager.close()
            except Exception:
                pass


def _write_plugin(root: Path) -> None:
    (root / "plugin.yaml").write_text(
        "\n".join((
            f"key: {PLUGIN_KEY}",
            "name: Windows AppContainer Smoke",
            "version: 1.0.0",
            "entrypoint: smoke_plugin:register",
            "provides: [tools]",
            "enabled_by_default: true",
        )),
        encoding="utf-8",
    )
    (root / "smoke_plugin.py").write_text(
        """from pathlib import Path
import socket
import subprocess
import sys


def _blocked(callback):
    try:
        callback()
    except Exception as exc:
        return True, type(exc).__name__
    return False, ""


def register(ctx):
    from luna_agent_plugin_sdk import ToolEntry

    secret_path = Path(ctx.config["secret_path"])
    outside_write_path = Path(ctx.config["outside_write_path"])

    async def probe():
        data_file = Path.cwd() / "probe-data.txt"
        data_file.write_text("worker-data", encoding="utf-8")
        outside_read_blocked, read_error = _blocked(
            lambda: secret_path.read_text(encoding="utf-8")
        )
        outside_write_blocked, write_error = _blocked(
            lambda: outside_write_path.write_text("escaped", encoding="utf-8")
        )

        def connect_network():
            connection = socket.create_connection(("1.1.1.1", 443), timeout=1.0)
            connection.close()

        network_blocked, network_error = _blocked(connect_network)
        child_process_blocked, child_error = _blocked(
            lambda: subprocess.run(
                [sys.executable, "-c", "print('child')"],
                check=True,
                capture_output=True,
                timeout=2.0,
            )
        )
        return {
            "data_write_ok": data_file.read_text(encoding="utf-8") == "worker-data",
            "outside_read_blocked": outside_read_blocked,
            "outside_write_blocked": outside_write_blocked,
            "network_blocked": network_blocked,
            "child_process_blocked": child_process_blocked,
            "errors": {
                "read": read_error,
                "write": write_error,
                "network": network_error,
                "child": child_error,
            },
        }

    ctx.register.tool(ToolEntry(
        name="windows_appcontainer_probe",
        description="Probe native AppContainer boundaries",
        schema={"type": "object", "properties": {}},
        handler=probe,
    ))
""",
        encoding="utf-8",
    )


def _profile_removed(profile_name: str) -> bool:
    import winreg

    kernel32 = ctypes.windll.kernel32
    userenv = ctypes.windll.userenv
    advapi32 = ctypes.windll.advapi32
    _configure_winapi(kernel32, userenv, advapi32)
    sid = ctypes.c_void_p()
    result = userenv.DeriveAppContainerSidFromAppContainerName(
        profile_name,
        ctypes.byref(sid),
    )
    if int(result) != 0:
        return True
    sid_text = wintypes.LPWSTR()
    try:
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError()
        mapping = (
            r"Software\Classes\Local Settings\Software\Microsoft\Windows"
            r"\CurrentVersion\AppContainer\Mappings" + "\\" + sid_text.value
        )
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, mapping):
                return False
        except FileNotFoundError:
            return True
    finally:
        if sid_text:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        if sid:
            advapi32.FreeSid(sid)


if __name__ == "__main__":
    raise SystemExit(main())
