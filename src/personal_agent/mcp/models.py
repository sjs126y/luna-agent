"""Stable Lumora models for MCP configuration and runtime boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlparse


class MCPTransport(str, Enum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class MCPRuntimeState(str, Enum):
    DISABLED = "disabled"
    STOPPED = "stopped"
    CONNECTING = "connecting"
    READY = "ready"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    CIRCUIT_OPEN = "circuit_open"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: MCPTransport = MCPTransport.STDIO
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers_env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    connect_timeout_seconds: float = 15.0
    call_timeout_seconds: float = 120.0
    allow_insecure_http: bool = False
    allow_private_network: bool = False
    allow_network: bool = False
    max_tools: int = 256
    max_tool_pages: int = 20
    max_schema_bytes: int = 65536
    max_result_chars: int = 100000
    max_artifact_bytes: int = 1048576

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "MCPServerConfig":
        command = str(value.get("command") or "").strip()
        url = str(value.get("url") or "").strip()
        raw_transport = str(value.get("transport") or "").strip().lower()
        if not raw_transport:
            raw_transport = MCPTransport.STREAMABLE_HTTP.value if url and not command else MCPTransport.STDIO.value
        try:
            transport = MCPTransport(raw_transport)
        except ValueError as exc:
            raise ValueError(f"Unsupported MCP transport: {raw_transport}") from exc

        if transport == MCPTransport.STDIO and not command:
            raise ValueError("stdio MCP server requires command")
        if transport == MCPTransport.STREAMABLE_HTTP:
            parsed_url = urlparse(url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                raise ValueError("streamable_http MCP server requires an http(s) URL")

        name = str(value.get("name") or command or url or "unknown").strip()
        return cls(
            name=name,
            transport=transport,
            command=command,
            args=_string_list(value.get("args")),
            env=_string_dict(value.get("env")),
            url=url,
            headers_env=_string_dict(value.get("headers_env")),
            enabled=bool(value.get("enabled", True)),
            connect_timeout_seconds=_positive_float(value.get("connect_timeout_seconds"), 15.0),
            call_timeout_seconds=_positive_float(value.get("call_timeout_seconds"), 120.0),
            allow_insecure_http=bool(value.get("allow_insecure_http", False)),
            allow_private_network=bool(value.get("allow_private_network", False)),
            allow_network=bool(value.get("allow_network", False)),
            max_tools=_positive_int(value.get("max_tools"), 256),
            max_tool_pages=_positive_int(value.get("max_tool_pages"), 20),
            max_schema_bytes=_positive_int(value.get("max_schema_bytes"), 65536),
            max_result_chars=_positive_int(value.get("max_result_chars"), 100000),
            max_artifact_bytes=_positive_int(value.get("max_artifact_bytes"), 1048576),
        )


@dataclass(frozen=True)
class MCPServerInfo:
    name: str
    version: str = ""
    protocol_version: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPToolSpec:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPContentBlock:
    type: str
    text: str = ""
    mime_type: str = ""
    data: str = ""
    uri: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPCallResult:
    text: str = ""
    content: list[MCPContentBlock] = field(default_factory=list)
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("MCP args must be a list")
    return [str(item) for item in value]


def _string_dict(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("MCP environment and header mappings must be objects")
    return {str(key): str(item) for key, item in value.items()}


def _positive_float(value: Any, default: float) -> float:
    if value in (None, ""):
        return default
    result = float(value)
    if result <= 0:
        raise ValueError("MCP timeouts must be positive")
    return result


def _positive_int(value: Any, default: int) -> int:
    if value in (None, ""):
        return default
    result = int(value)
    if result <= 0:
        raise ValueError("MCP limits must be positive")
    return result
