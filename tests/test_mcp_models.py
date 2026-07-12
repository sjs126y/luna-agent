from __future__ import annotations

import pytest

from personal_agent.mcp.models import MCPServerConfig, MCPTransport


def test_stdio_config_remains_backward_compatible():
    config = MCPServerConfig.from_mapping({"name": "local", "command": "python", "args": ["server.py"]})

    assert config.transport == MCPTransport.STDIO
    assert config.command == "python"
    assert config.args == ["server.py"]


def test_http_config_can_be_inferred_from_url():
    config = MCPServerConfig.from_mapping({
        "name": "remote",
        "url": "https://example.com/mcp",
        "headers_env": {"Authorization": "REMOTE_TOKEN"},
    })

    assert config.transport == MCPTransport.STREAMABLE_HTTP
    assert config.headers_env == {"Authorization": "REMOTE_TOKEN"}


def test_invalid_transport_is_rejected():
    with pytest.raises(ValueError, match="Unsupported MCP transport"):
        MCPServerConfig.from_mapping({"name": "bad", "transport": "websocket"})
