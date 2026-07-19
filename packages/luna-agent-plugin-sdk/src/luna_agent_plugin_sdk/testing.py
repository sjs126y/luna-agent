"""Host-free contract harness for plugin registration tests."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import importlib
from pathlib import Path
import sys
from types import MappingProxyType
from typing import Any, Iterator
from uuid import uuid4


@dataclass(slots=True)
class RegistrationSnapshot:
    tools: list[Any] = field(default_factory=list)
    skills: list[Any] = field(default_factory=list)
    skill_directories: list[str] = field(default_factory=list)
    workflows: list[Any] = field(default_factory=list)
    platforms: list[Any] = field(default_factory=list)
    mcp_servers: list[Any] = field(default_factory=list)
    mcp_files: list[str] = field(default_factory=list)
    hooks: list[dict[str, Any]] = field(default_factory=list)
    commands: list[Any] = field(default_factory=list)
    memory_providers: list[dict[str, Any]] = field(default_factory=list)
    active: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tools": len(self.tools),
            "skills": len(self.skills),
            "skill_directories": list(self.skill_directories),
            "workflows": len(self.workflows),
            "platforms": len(self.platforms),
            "mcp_servers": len(self.mcp_servers),
            "mcp_files": list(self.mcp_files),
            "hooks": len(self.hooks),
            "commands": len(self.commands),
            "memory_providers": len(self.memory_providers),
            "active": len(self.active),
        }


class FakeRegistrationPort:
    def __init__(self, context: "FakePluginRuntimeContext") -> None:
        self.context = context
        self.snapshot = RegistrationSnapshot()

    def tool(self, entry: Any) -> None:
        self.snapshot.tools.append(entry)

    def skill(self, entry: Any) -> None:
        self.snapshot.skills.append(entry)

    def skills(self, relative_path: str | Path = "skills") -> int:
        path = self.context.resolve_path(relative_path)
        if not path.is_dir():
            raise ValueError(f"Plugin skills directory does not exist: {relative_path}")
        count = sum(1 for item in path.rglob("SKILL.md") if item.is_file())
        self.snapshot.skill_directories.append(str(relative_path))
        return count

    def workflow(self, definition: Any) -> None:
        self.snapshot.workflows.append(definition)

    def platform(self, entry: Any) -> None:
        self.snapshot.platforms.append(entry)

    def mcp_server(self, config: Any) -> None:
        self.snapshot.mcp_servers.append(config)

    def mcp(self, relative_path: str | Path = "mcp.yaml") -> int:
        path = self.context.resolve_path(relative_path)
        if not path.is_file():
            raise ValueError(f"Plugin MCP configuration does not exist: {relative_path}")
        self.snapshot.mcp_files.append(str(relative_path))
        return 1

    def hook(self, event: Any, callback: Any, priority: int = 100, **kwargs: Any) -> None:
        if not callable(callback):
            raise TypeError("Plugin hook callback must be callable")
        self.snapshot.hooks.append({
            "event": str(getattr(event, "value", event)),
            "callback": callback,
            "priority": int(priority),
            **kwargs,
        })

    def command(self, entry: Any) -> None:
        self.snapshot.commands.append(entry)

    def memory_provider(self, *, name: str, factory: Any, validator: Any) -> None:
        self.snapshot.memory_providers.append({
            "name": name,
            "factory": factory,
            "validator": validator,
        })

    def active(self, *, run: Any, resources: Any = None, **kwargs: Any) -> None:
        if not callable(run):
            raise TypeError("Active plugin run must be callable")
        self.snapshot.active.append({"run": run, "resources": resources, **kwargs})


class FakePluginRuntimeContext:
    def __init__(
        self,
        root: str | Path,
        *,
        plugin_key: str = "contract/plugin",
        config: dict[str, Any] | None = None,
    ) -> None:
        self.plugin_key = plugin_key
        self.generation_id = f"gen_{uuid4().hex[:12]}"
        self.runtime_instance_id = f"runtime_{uuid4().hex[:12]}"
        self.root = Path(root).resolve()
        self.config = MappingProxyType(dict(config or {}))
        self.register = FakeRegistrationPort(self)

    def parse_config(self, model_type: Any) -> Any:
        validator = getattr(model_type, "model_validate", None)
        if not callable(validator):
            raise TypeError("Plugin config model must provide model_validate()")
        return validator(dict(self.config))

    def get_env(self, name: str, default: str = "") -> str:
        return default

    def resolve_path(self, relative_path: str | Path) -> Path:
        candidate = Path(relative_path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"Plugin path escapes package root: {relative_path}")
        return candidate


def run_plugin_contract(
    root: str | Path,
    entrypoint: str,
    *,
    plugin_key: str = "contract/plugin",
    config: dict[str, Any] | None = None,
) -> RegistrationSnapshot:
    """Import and execute one plugin entrypoint against a host-free context."""
    root = Path(root).resolve()
    module_name, _, function_name = str(entrypoint).partition(":")
    function_name = function_name or "register"
    context = FakePluginRuntimeContext(root, plugin_key=plugin_key, config=config)
    with _plugin_import_path(root):
        sys.modules.pop(module_name, None)
        try:
            module = importlib.import_module(module_name)
            register = getattr(module, function_name, None)
            if not callable(register):
                raise TypeError(f"Plugin entrypoint is not callable: {entrypoint}")
            result = register(context)
            if result is not None:
                raise TypeError("Plugin register() must return None")
        finally:
            sys.modules.pop(module_name, None)
    return context.register.snapshot


@contextmanager
def _plugin_import_path(root: Path) -> Iterator[None]:
    value = str(root)
    sys.path.insert(0, value)
    try:
        yield
    finally:
        try:
            sys.path.remove(value)
        except ValueError:
            pass
