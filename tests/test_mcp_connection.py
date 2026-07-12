from __future__ import annotations

import sys
from pathlib import Path

import pytest

from personal_agent.mcp.connection import SDKMCPConnection
from personal_agent.mcp.models import MCPServerConfig


MOCK_SERVER = r"""
import json, sys

TOOLS = [{"name":"echo","description":"Echo text","inputSchema":{"type":"object","properties":{"text":{"type":"string"}}}}]

def respond(request_id, result):
    sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":request_id,"result":result}) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        respond(request_id, {"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"sdk-mock","version":"1.0"}})
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        respond(request_id, {"tools":TOOLS})
    elif method == "tools/call":
        text = request.get("params", {}).get("arguments", {}).get("text", "")
        respond(request_id, {"content":[{"type":"text","text":text}],"isError":False})
"""


@pytest.fixture
def sdk_server(tmp_path: Path) -> Path:
    path = tmp_path / "sdk_mcp_server.py"
    path.write_text(MOCK_SERVER, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_sdk_stdio_connection_discovers_and_calls_tools(sdk_server: Path):
    connection = SDKMCPConnection(MCPServerConfig.from_mapping({
        "name": "mock",
        "command": sys.executable,
        "args": ["-u", str(sdk_server)],
    }))
    try:
        info = await connection.connect()
        tools = await connection.list_tools()
        result = await connection.call_tool("echo", {"text": "hello"})

        assert info.name == "sdk-mock"
        assert [tool.name for tool in tools] == ["echo"]
        assert result.text == "hello"
        assert result.content[0].type == "text"
        assert result.is_error is False
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_sdk_connection_close_is_idempotent(sdk_server: Path):
    connection = SDKMCPConnection(MCPServerConfig.from_mapping({
        "name": "mock",
        "command": sys.executable,
        "args": ["-u", str(sdk_server)],
    }))
    await connection.connect()

    await connection.close()
    await connection.close()

    assert connection.connected is False


@pytest.mark.asyncio
async def test_sdk_connection_rejects_calls_before_connect():
    connection = SDKMCPConnection(MCPServerConfig(name="mock", command=sys.executable))

    with pytest.raises(ConnectionError, match="not connected"):
        await connection.call_tool("echo", {})
