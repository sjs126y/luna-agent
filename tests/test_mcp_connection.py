from __future__ import annotations

import sys
from pathlib import Path

import pytest
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

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


@pytest.mark.asyncio
async def test_sdk_streamable_http_connection():
    server = FastMCP(
        "http-mock",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @server.tool()
    def echo(text: str) -> str:
        return text

    app = server.streamable_http_app()
    captured_headers = {}

    def client_factory(headers, connect_timeout, call_timeout):
        captured_headers.update(headers)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers=headers,
            timeout=httpx.Timeout(call_timeout, connect=connect_timeout),
            follow_redirects=False,
        )

    connection = SDKMCPConnection(
        MCPServerConfig.from_mapping({
            "name": "remote",
            "transport": "streamable_http",
            "url": "http://testserver/mcp",
            "headers_env": {"Authorization": "REMOTE_MCP_TOKEN"},
        }),
        http_client_factory=client_factory,
        env_values={"REMOTE_MCP_TOKEN": "secret-value"},
    )
    async with app.router.lifespan_context(app):
        try:
            info = await connection.connect()
            result = await connection.call_tool("echo", {"text": "over http"})

            assert info.name == "http-mock"
            assert result.text == "over http"
            assert captured_headers == {"Authorization": "secret-value"}
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_sdk_http_connection_requires_configured_header_env():
    connection = SDKMCPConnection(MCPServerConfig.from_mapping({
        "name": "remote",
        "transport": "streamable_http",
        "url": "https://example.com/mcp",
        "headers_env": {"Authorization": "MISSING_MCP_TOKEN"},
    }), env_values={})

    with pytest.raises(ValueError, match="MISSING_MCP_TOKEN"):
        await connection.connect()
