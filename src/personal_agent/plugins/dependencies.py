"""Dependency graph and compatibility diagnostics for plugin manifests."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Any

from lumora_plugin_sdk import PLUGIN_API_VERSION, SDK_VERSION
from packaging.specifiers import SpecifierSet
from packaging.version import Version

HOST_CAPABILITIES = {
    "artifact.create",
    "conversation.submit",
    "delivery.send",
    "events.publish",
    "llm.call",
    "storage.read_write",
    "tasks.create",
}


@dataclass(frozen=True, slots=True)
class DependencyIssue:
    code: str
    message: str
    path: str
    severity: str = "error"

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "severity": self.severity,
        }


@dataclass(slots=True)
class DependencyReport:
    plugin_key: str
    dependencies: list[str] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)
    issues: list[DependencyIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dependencies": list(self.dependencies),
            "dependents": list(self.dependents),
            "issues": [item.as_dict() for item in self.issues],
        }


class PluginDependencyResolver:
    def __init__(self, manager) -> None:
        self.manager = manager

    def report(self, key: str) -> DependencyReport:
        plugin = self.manager._plugins[key]
        manifest = plugin.manifest
        requires = manifest.requires
        report = DependencyReport(
            plugin_key=key,
            dependencies=[item.key for item in requires.plugins],
            dependents=self.dependents(key),
        )
        self._check_version(
            report,
            requires.lumora,
            _package_version("personal-agent", "0.1.0"),
            "HOST_VERSION_INCOMPATIBLE",
            "requires.lumora",
            "Lumora",
        )
        self._check_version(
            report,
            requires.sdk,
            SDK_VERSION,
            "SDK_VERSION_INCOMPATIBLE",
            "requires.sdk",
            "lumora-plugin-sdk",
        )
        self._check_version(
            report,
            manifest.plugin_api,
            PLUGIN_API_VERSION,
            "PLUGIN_API_INCOMPATIBLE",
            "plugin_api",
            "Plugin API",
        )
        cycle = self.cycle_for(key)
        if cycle:
            report.issues.append(DependencyIssue(
                "PLUGIN_DEPENDENCY_CYCLE",
                "Plugin dependency cycle: " + " -> ".join(cycle),
                "requires.plugins",
            ))
        for index, requirement in enumerate(requires.plugins):
            dependency = self.manager._plugins.get(requirement.key)
            path = f"requires.plugins[{index}]"
            if dependency is None:
                report.issues.append(DependencyIssue(
                    "PLUGIN_DEPENDENCY_MISSING",
                    f"Missing plugin dependency: {requirement.key}",
                    path,
                ))
                continue
            if not dependency.enabled:
                report.issues.append(DependencyIssue(
                    "PLUGIN_DEPENDENCY_DISABLED",
                    f"Plugin dependency is disabled: {requirement.key}",
                    path,
                ))
            if requirement.version and Version(dependency.manifest.version) not in SpecifierSet(requirement.version):
                report.issues.append(DependencyIssue(
                    "PLUGIN_DEPENDENCY_VERSION_MISMATCH",
                    f"{requirement.key} {dependency.manifest.version} does not satisfy {requirement.version}",
                    f"{path}.version",
                ))
        available = self.available_capabilities(excluding=key)
        for index, capability in enumerate(requires.capabilities):
            if capability not in available:
                report.issues.append(DependencyIssue(
                    "CAPABILITY_DEPENDENCY_MISSING",
                    f"Missing capability dependency: {capability}",
                    f"requires.capabilities[{index}]",
                ))
        available_mcp = self.available_mcp_tools()
        for server, tools in requires.mcp_tools.items():
            for tool in tools:
                if (server, tool) not in available_mcp:
                    report.issues.append(DependencyIssue(
                        "MCP_TOOL_DEPENDENCY_UNAVAILABLE",
                        f"MCP tool is not currently available: {server}.{tool}",
                        f"requires.mcp_tools.{server}",
                        severity="warning",
                    ))
        return report

    def load_order(self, keys: list[str]) -> list[str]:
        selected = set(keys)
        state: dict[str, int] = {}
        ordered: list[str] = []

        def visit(key: str) -> None:
            marker = state.get(key, 0)
            if marker == 2:
                return
            if marker == 1:
                return
            state[key] = 1
            plugin = self.manager._plugins.get(key)
            if plugin is not None:
                for requirement in plugin.manifest.requires.plugins:
                    if requirement.key in selected:
                        visit(requirement.key)
            state[key] = 2
            ordered.append(key)

        for key in sorted(selected):
            visit(key)
        return ordered

    def dependents(self, key: str, *, enabled_only: bool = False) -> list[str]:
        result = []
        for plugin in self.manager._plugins.values():
            if enabled_only and not plugin.enabled:
                continue
            if any(item.key == key for item in plugin.manifest.requires.plugins):
                result.append(plugin.key)
        return sorted(result)

    def cycle_for(self, start: str) -> list[str]:
        path: list[str] = []
        visiting: set[str] = set()

        def visit(key: str) -> list[str]:
            if key in visiting:
                index = path.index(key)
                return [*path[index:], key]
            plugin = self.manager._plugins.get(key)
            if plugin is None:
                return []
            visiting.add(key)
            path.append(key)
            for requirement in plugin.manifest.requires.plugins:
                cycle = visit(requirement.key)
                if cycle:
                    return cycle
            path.pop()
            visiting.remove(key)
            return []

        return visit(start)

    def available_capabilities(self, *, excluding: str = "") -> set[str]:
        result = set(HOST_CAPABILITIES)
        for plugin in self.manager._plugins.values():
            if plugin.key == excluding or not plugin.enabled:
                continue
            for name in plugin.manifest.provides:
                result.add(f"plugin:{plugin.key}:{name}")
        snapshot = self.manager.capability_store.current
        for kind, routes in snapshot.routes.items():
            for name in routes:
                result.add(f"{kind.value}:{name}")
        return result

    def available_mcp_tools(self) -> set[tuple[str, str]]:
        from personal_agent.tools.registry import tool_registry

        result = set()
        for name in tool_registry.all_names:
            if not name.startswith("mcp__"):
                continue
            parts = name.split("__", 2)
            if len(parts) == 3:
                result.add((parts[1], parts[2]))
        return result

    @staticmethod
    def _check_version(
        report: DependencyReport,
        specifier: str,
        current: str,
        code: str,
        path: str,
        label: str,
    ) -> None:
        if specifier and Version(current) not in SpecifierSet(specifier):
            report.issues.append(DependencyIssue(
                code,
                f"{label} {current} does not satisfy {specifier}",
                path,
            ))


def _package_version(name: str, fallback: str) -> str:
    try:
        return package_version(name)
    except PackageNotFoundError:
        return fallback
