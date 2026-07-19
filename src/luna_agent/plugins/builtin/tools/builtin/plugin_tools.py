"""Discoverable tools for inspecting, building, and managing external plugins."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from luna_agent.plugins.devtools import (
    capability_catalog,
    contract_test,
    package_plugin,
    validate_plugin_source,
)
from luna_agent.tools.entry import ToolEntry, ToolHandlerOutput
from luna_agent.tools.registry import tool_registry
from luna_agent.tools.runtime_context import current_tool_agent
from luna_agent.tools.sandbox import get_sandbox


def _result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _error(reason: str, message: str) -> ToolHandlerOutput:
    return ToolHandlerOutput(
        text=_result({"ok": False, "error": message, "reason_code": reason}),
        metadata={"reason_code": reason},
        is_error=True,
    )


def _live_manager():
    agent = current_tool_agent()
    manager = getattr(agent, "_plugin_manager", None) if agent is not None else None
    if manager is None:
        raise RuntimeError("Live PluginManager is unavailable in this runtime")
    return manager


def _allowed_path(raw: str, *, access: str, must_exist: bool) -> Path:
    if not str(raw or "").strip():
        raise ValueError("A local path is required")
    requested = Path(str(raw)).expanduser()
    if requested.is_symlink():
        raise ValueError(f"Symbolic link paths are not allowed: {raw}")
    full = requested.resolve() if requested.is_absolute() else get_sandbox().resolve(str(requested))
    error = get_sandbox().check_path(full, access=access)
    if error:
        raise ValueError(error)
    if must_exist and not full.exists():
        raise ValueError(f"Path does not exist: {full}")
    return full


def _external_plugin(manager, key: str):
    if not str(key or "").strip():
        raise ValueError("plugin_key is required for this action")
    if not manager.list_plugins():
        manager.discover()
    plugin = next((item for item in manager.list_plugins() if item.key == key), None)
    if plugin is None:
        raise KeyError(f"Plugin not found: {key}")
    if plugin.manifest.source == "builtin":
        raise ValueError(f"Built-in plugins cannot be managed by Agent tools: {key}")
    return plugin


def _plugin_summary(manager, plugin, *, removed: bool = False) -> dict[str, Any]:
    if removed:
        operations = manager.queries.operations(key=plugin.key, limit=1)
        return {
            "ok": True,
            "plugin_key": plugin.key,
            "name": plugin.manifest.name,
            "version": plugin.manifest.version,
            "status": "UNINSTALLED",
            "runtime_state": plugin.runtime_state.value,
            "generation_id": plugin.generation_id,
            "runtime_instance_id": plugin.runtime_instance_id,
            "snapshot_revision": manager.capability_store.current.revision,
            "package_digest": plugin.package_digest,
            "active_enabled": False,
            "active": {},
            "error": "",
            "latest_operation": operations[0] if operations else {},
            "data_preserved": True,
            "effective_next_turn": True,
        }
    report = manager.queries.plugin_info(plugin.key)
    return {
        "ok": report.get("status") not in {"ERROR", "BLOCKED"},
        "plugin_key": plugin.key,
        "name": report.get("name", ""),
        "version": report.get("version", ""),
        "status": report.get("status", ""),
        "runtime_state": report.get("runtime_state", ""),
        "generation_id": report.get("generation_id", ""),
        "runtime_instance_id": report.get("runtime_instance_id", ""),
        "snapshot_revision": report.get("snapshot_revision", 0),
        "package_digest": report.get("package_digest", ""),
        "active_enabled": report.get("active_enabled", False),
        "active": report.get("active", {}),
        "error": report.get("error", ""),
        "latest_operation": report.get("latest_operation", {}),
        "effective_next_turn": True,
    }


async def plugin_inspect(
    action: str,
    plugin_key: str = "",
    operation_id: str = "",
    limit: int = 20,
) -> str | ToolHandlerOutput:
    try:
        manager = _live_manager()
        action = str(action or "").strip().lower()
        if action == "list":
            return _result({"ok": True, "plugins": manager.queries.list_plugins()})
        if action == "info":
            if not plugin_key:
                raise ValueError("plugin_key is required for info")
            return _result({"ok": True, "plugin": manager.queries.plugin_info(plugin_key)})
        if action == "versions":
            if not plugin_key:
                raise ValueError("plugin_key is required for versions")
            return _result({"ok": True, "versions": manager.queries.versions(plugin_key)})
        if action == "operations":
            return _result({
                "ok": True,
                "operations": manager.queries.operations(key=plugin_key, limit=max(1, min(limit, 100))),
            })
        if action == "operation":
            if not operation_id:
                raise ValueError("operation_id is required for operation")
            operation = manager.queries.operation(operation_id)
            if operation is None:
                raise KeyError(f"Plugin operation not found: {operation_id}")
            return _result({"ok": True, "operation": operation})
        if action == "capabilities":
            return _result({"ok": True, "capabilities": capability_catalog()})
        raise ValueError(f"Unsupported plugin_inspect action: {action}")
    except Exception as exc:
        return _error("plugin_inspect_failed", f"{type(exc).__name__}: {exc}")


async def plugin_build(
    action: str,
    source: str,
    output: str = "",
) -> str | ToolHandlerOutput:
    try:
        action = str(action or "").strip().lower()
        source_path = _allowed_path(source, access="read", must_exist=True)
        if action == "validate":
            report = await asyncio.to_thread(validate_plugin_source, source_path)
            return _result(report)
        if action == "test":
            # The contract harness temporarily changes sys.path while importing the
            # plugin. Keep that import on the event-loop thread to avoid import-lock
            # deadlocks with modules already loading in the executor pool.
            report = contract_test(source_path)
            return _result(report)
        if action == "package":
            validation = await asyncio.to_thread(validate_plugin_source, source_path)
            if not validation["ok"]:
                return _error("plugin_validation_failed", _result(validation))
            default_name = (
                f"{validation['plugin_key'].replace('/', '-')}-{validation['version']}.zip"
            )
            target = _allowed_path(
                output or str(Path(validation["root"]).parent / default_name),
                access="write",
                must_exist=False,
            )
            artifact = await asyncio.to_thread(package_plugin, source_path, target)
            digest = await asyncio.to_thread(
                lambda: hashlib.sha256(artifact.read_bytes()).hexdigest()
            )
            return _result({
                "ok": True,
                "action": "package",
                "plugin_key": validation["plugin_key"],
                "version": validation["version"],
                "path": str(artifact),
                "sha256": digest,
                "size_bytes": artifact.stat().st_size,
            })
        raise ValueError(f"Unsupported plugin_build action: {action}")
    except Exception as exc:
        return _error("plugin_build_failed", f"{type(exc).__name__}: {exc}")


async def plugin_manage(
    action: str,
    plugin_key: str = "",
    source: str = "",
    digest: str = "",
    enable: bool = True,
) -> str | ToolHandlerOutput:
    try:
        manager = _live_manager()
        action = str(action or "").strip().lower()
        removed = False
        if action == "install":
            source_path = _allowed_path(source, access="read", must_exist=True)
            plugin = await manager.install_plugin_runtime(source_path, enable=enable)
        elif action == "enable":
            _external_plugin(manager, plugin_key)
            plugin = await manager.enable_plugin_runtime(plugin_key)
        elif action == "disable":
            _external_plugin(manager, plugin_key)
            plugin = await manager.disable_plugin_runtime(plugin_key)
        elif action == "reload":
            _external_plugin(manager, plugin_key)
            plugin = await manager.reload_plugin_runtime(plugin_key)
        elif action == "rollback":
            _external_plugin(manager, plugin_key)
            if not digest:
                raise ValueError("digest is required for rollback")
            plugin = await manager.rollback_plugin_runtime(plugin_key, digest)
        elif action == "uninstall":
            _external_plugin(manager, plugin_key)
            plugin = await manager.uninstall_plugin_runtime(
                plugin_key,
                purge_data=False,
                force=False,
            )
            removed = True
        else:
            raise ValueError(f"Unsupported plugin_manage action: {action}")
        return _result(_plugin_summary(manager, plugin, removed=removed))
    except Exception as exc:
        return _error("plugin_manage_failed", f"{type(exc).__name__}: {exc}")


def _inspect_precheck(input_: dict[str, Any]) -> str | None:
    if str(input_.get("action") or "") not in {
        "list", "info", "versions", "operations", "operation", "capabilities",
    }:
        return "Unsupported plugin_inspect action."
    return None


def _blocked_path(raw: str) -> str | None:
    if not str(raw or "").strip():
        return None
    return get_sandbox().check_blocked_path(get_sandbox().resolve(str(raw)))


def _build_precheck(input_: dict[str, Any]) -> str | None:
    if str(input_.get("action") or "") not in {"validate", "test", "package"}:
        return "Unsupported plugin_build action."
    if not str(input_.get("source") or "").strip():
        return "A local plugin source path is required."
    return _blocked_path(str(input_.get("source") or "")) or _blocked_path(
        str(input_.get("output") or "")
    )


def _manage_precheck(input_: dict[str, Any]) -> str | None:
    action = str(input_.get("action") or "")
    if action not in {"install", "enable", "disable", "reload", "rollback", "uninstall"}:
        return "Unsupported plugin_manage action."
    if action == "install" and not str(input_.get("source") or "").strip():
        return "A local plugin source path is required for install."
    if action != "install" and not str(input_.get("plugin_key") or "").strip():
        return "plugin_key is required for this action."
    if action == "rollback" and not str(input_.get("digest") or "").strip():
        return "digest is required for rollback."
    if action == "install":
        return _blocked_path(str(input_.get("source") or ""))
    return None


def _build_resources(input_: dict[str, Any]) -> list:
    from luna_agent.security.models import ResourceRequirement

    source = get_sandbox().resolve(str(input_.get("source") or "."))
    resources = [ResourceRequirement("filesystem", str(source), "read", "plugin source")]
    if str(input_.get("action") or "") == "package":
        output = str(input_.get("output") or "").strip()
        target = get_sandbox().resolve(output) if output else source.parent
        resources.append(ResourceRequirement("filesystem", str(target), "write", "plugin package"))
    return resources


def _manage_resources(input_: dict[str, Any]) -> list:
    if str(input_.get("action") or "") != "install":
        return []
    from luna_agent.security.models import ResourceRequirement

    source = get_sandbox().resolve(str(input_.get("source") or "."))
    return [ResourceRequirement("filesystem", str(source), "read", "plugin install source")]


plugin_inspect_entry = ToolEntry(
    name="plugin_inspect",
    description=(
        "Inspect Luna Agent plugins, installed versions, operations, and public plugin "
        "capabilities. Use this read-only tool before building or managing a plugin."
    ),
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [
                "list", "info", "versions", "operations", "operation", "capabilities",
            ]},
            "plugin_key": {"type": "string"},
            "operation_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
    handler=plugin_inspect,
    toolset="plugin",
    permission_category="read",
    tags=["plugin", "inspect", "versions", "operations", "capabilities"],
    risk_level="low",
    approval_mode="auto",
    precheck=_inspect_precheck,
    idempotent=True,
)

plugin_build_entry = ToolEntry(
    name="plugin_build",
    description=(
        "Validate, contract-test, or deterministically package a local Luna Agent plugin. "
        "This tool does not edit plugin source code."
    ),
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["validate", "test", "package"]},
            "source": {"type": "string", "description": "Allowed local plugin directory or manifest path."},
            "output": {"type": "string", "description": "Optional output ZIP path for package."},
        },
        "required": ["action", "source"],
        "additionalProperties": False,
    },
    handler=plugin_build,
    toolset="plugin",
    permission_category="write",
    tags=["plugin", "validate", "test", "package", "build"],
    risk_level="high",
    approval_mode="prompt",
    precheck=_build_precheck,
    resource_resolver=_build_resources,
    idempotent=False,
    is_parallel_safe=False,
)

plugin_manage_entry = ToolEntry(
    name="plugin_manage",
    description=(
        "Install, enable, disable, reload, roll back, or uninstall an external Luna Agent "
        "plugin through the live PluginManager. Install accepts local directories, ZIP, or TAR only. "
        "Uninstall always preserves plugin data."
    ),
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [
                "install", "enable", "disable", "reload", "rollback", "uninstall",
            ]},
            "plugin_key": {"type": "string"},
            "source": {"type": "string", "description": "Local directory, ZIP, or TAR for install."},
            "digest": {"type": "string", "description": "Installed package digest for rollback."},
            "enable": {"type": "boolean", "default": True},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
    handler=plugin_manage,
    toolset="plugin",
    permission_category="destructive",
    tags=["plugin", "install", "reload", "rollback", "uninstall", "hot-reload"],
    risk_level="high",
    approval_mode="prompt",
    precheck=_manage_precheck,
    resource_resolver=_manage_resources,
    idempotent=False,
    is_parallel_safe=False,
    is_destructive=True,
    timeout_seconds=120.0,
)


def register(ctx) -> None:
    ctx.register.tool(plugin_inspect_entry)
    ctx.register.tool(plugin_build_entry)
    ctx.register.tool(plugin_manage_entry)


for _entry in (plugin_inspect_entry, plugin_build_entry, plugin_manage_entry):
    if tool_registry.get(_entry.name) is None:
        tool_registry.register(_entry)
