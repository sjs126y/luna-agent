"""Generation-isolated imports for non-builtin plugin packages."""

from __future__ import annotations

import importlib
from importlib.machinery import ModuleSpec
from pathlib import Path
import re
import sys
from types import ModuleType


_ROOT_NAMESPACE = "luna_agent_plugin_generations"


def generation_module_namespace(plugin_key: str, runtime_instance_id: str) -> str:
    plugin_part = _identifier(plugin_key.replace("/", "__"))
    runtime_part = _identifier(runtime_instance_id.rpartition(":")[2] or runtime_instance_id)
    return f"{_ROOT_NAMESPACE}.{plugin_part}.g_{runtime_part}"


def import_generation_entrypoint(
    *,
    plugin_root: Path,
    entrypoint: str,
    namespace: str,
) -> tuple[ModuleType, object | None]:
    module_name, _, function_name = entrypoint.partition(":")
    _remove_bytecode_cache(plugin_root)
    importlib.invalidate_caches()
    search_paths = [plugin_root]
    if (plugin_root / "__init__.py").is_file():
        search_paths.append(plugin_root.parent)
    _ensure_namespace(namespace, search_paths)
    module = importlib.import_module(f"{namespace}.{module_name}")
    function = getattr(module, function_name) if function_name else None
    return module, function


def cleanup_generation_namespace(namespace: str) -> None:
    if not namespace:
        return
    for module_name in tuple(sys.modules):
        if module_name == namespace or module_name.startswith(f"{namespace}."):
            sys.modules.pop(module_name, None)
    importlib.invalidate_caches()


def _ensure_namespace(namespace: str, search_paths: list[Path]) -> None:
    parts = namespace.split(".")
    for index in range(1, len(parts) + 1):
        name = ".".join(parts[:index])
        module = sys.modules.get(name)
        if module is None:
            module = ModuleType(name)
            module.__package__ = name
            module.__spec__ = ModuleSpec(name, loader=None, is_package=True)
            module.__path__ = []  # type: ignore[attr-defined]
            sys.modules[name] = module
        if index == len(parts):
            module.__path__ = [str(path) for path in search_paths]  # type: ignore[attr-defined]


def _identifier(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_]", "_", str(value or ""))
    if not normalized:
        return "plugin"
    if normalized[0].isdigit():
        normalized = f"p_{normalized}"
    return normalized


def _remove_bytecode_cache(plugin_root: Path) -> None:
    for cached in plugin_root.rglob("__pycache__/*.pyc"):
        try:
            cached.unlink()
        except OSError:
            pass
