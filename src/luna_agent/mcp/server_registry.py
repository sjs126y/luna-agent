"""Plugin-owned MCP server configuration registry."""

from __future__ import annotations

from dataclasses import dataclass

from luna_agent.mcp.models import MCPServerConfig


@dataclass(frozen=True)
class RegisteredMCPServer:
    plugin_key: str
    config: MCPServerConfig
    runtime_instance_id: str = ""


class MCPServerRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, RegisteredMCPServer] = {}
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    def register(
        self,
        plugin_key: str,
        config: MCPServerConfig | dict,
        runtime_instance_id: str = "",
    ) -> MCPServerConfig:
        normalized = config if isinstance(config, MCPServerConfig) else MCPServerConfig.from_mapping(config)
        existing = self._entries.get(normalized.name)
        if existing is not None:
            if existing.plugin_key != plugin_key:
                raise ValueError(
                    f"MCP server '{normalized.name}' is already registered by plugin "
                    f"'{existing.plugin_key}'"
                )
            if existing.config == normalized:
                return normalized
            if existing.runtime_instance_id == runtime_instance_id:
                raise ValueError(f"MCP server '{normalized.name}' is already registered by this plugin generation")
            self._entries[normalized.name] = RegisteredMCPServer(
                plugin_key,
                normalized,
                runtime_instance_id,
            )
            self._revision += 1
            return normalized
        self._entries[normalized.name] = RegisteredMCPServer(
            plugin_key,
            normalized,
            runtime_instance_id,
        )
        self._revision += 1
        return normalized

    def unregister_plugin(self, plugin_key: str) -> list[str]:
        names = [name for name, item in self._entries.items() if item.plugin_key == plugin_key]
        for name in names:
            del self._entries[name]
        if names:
            self._revision += 1
        return sorted(names)

    def configs(self) -> list[MCPServerConfig]:
        return [item.config for item in self._entries.values()]

    def owner_for(self, name: str) -> str:
        entry = self._entries.get(name)
        return entry.plugin_key if entry is not None else ""

    def snapshot(self) -> tuple[dict[str, RegisteredMCPServer], int]:
        return dict(self._entries), self._revision

    def restore(self, snapshot: tuple[dict[str, RegisteredMCPServer], int]) -> None:
        entries, revision = snapshot
        self._entries = dict(entries)
        self._revision = revision
