from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from luna_agent.mcp.connection import SDKMCPConnection
from luna_agent.mcp.models import MCPServerConfig


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
            "url": "http://localhost/mcp",
            "allow_insecure_http": True,
            "allow_private_network": True,
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


def test_mcp_http_requires_https_and_explicit_private_opt_in():
    from luna_agent.mcp.connection import _validate_http_target

    insecure = MCPServerConfig.from_mapping({
        "name": "remote",
        "url": "http://example.com/mcp",
    })
    private = MCPServerConfig.from_mapping({
        "name": "local",
        "url": "https://127.0.0.1/mcp",
    })
    opted_in = MCPServerConfig.from_mapping({
        "name": "local",
        "url": "http://127.0.0.1/mcp",
        "allow_insecure_http": True,
        "allow_private_network": True,
    })

    with pytest.raises(ValueError, match="requires HTTPS"):
        _validate_http_target(insecure)
    with pytest.raises(ValueError, match="Unsafe MCP HTTP endpoint"):
        _validate_http_target(private)
    _validate_http_target(opted_in)


def test_stdio_connection_uses_process_sandbox(tmp_path, monkeypatch):
    from luna_agent.tools import process_sandbox

    monkeypatch.setattr(
        process_sandbox,
        "process_sandbox_capabilities",
        lambda: {
            "bwrap_available": True,
            "bwrap_path": "/usr/bin/bwrap",
            "network_namespace_available": True,
        },
    )
    connection = SDKMCPConnection(
        MCPServerConfig.from_mapping({
            "name": "mock",
            "command": "python",
            "args": ["server.py"],
        }),
        process_backend="bwrap",
        sandbox_roots=[tmp_path],
        work_dir=tmp_path,
    )

    params = connection._stdio_parameters()

    assert params.command == "/usr/bin/bwrap"
    assert "--ro-bind" in params.args
    assert "--unshare-net" in params.args
    assert params.env["HOME"] == str(tmp_path.resolve())


def test_mcp_result_payloads_are_bounded():
    from luna_agent.mcp.connection import _normalize_call_result

    class Block:
        def __init__(self, value):
            self.value = value

        def model_dump(self, **_kwargs):
            return dict(self.value)

    result = SimpleNamespace(
        content=[
            Block({"type": "text", "text": "x" * 100}),
            Block({"type": "image", "mimeType": "image/png", "data": "a" * 100}),
        ],
        structuredContent={"large": "y" * 100},
        isError=False,
    )
    config = MCPServerConfig(
        name="mock",
        command="python",
        max_result_chars=32,
        max_artifact_bytes=8,
    )

    normalized = _normalize_call_result(result, config)

    assert len(normalized.text) <= 32
    assert normalized.content[1].data == ""
    assert normalized.content[1].metadata == {"truncated": True}
    assert normalized.metadata["structured_content_truncated"] is True


def test_mcp_text_links_are_promoted_only_from_configured_artifact_root(tmp_path):
    from luna_agent.mcp.connection import _normalize_call_result

    output = tmp_path / "playwright"
    output.mkdir()
    screenshot = output / "example.png"
    screenshot.write_bytes(b"png-data")
    (output / "snapshot.yml").write_text("private", encoding="utf-8")

    class Block:
        def model_dump(self, **_kwargs):
            return {
                "type": "text",
                "text": (
                    "### Result\n- [Screenshot](./example.png)\n"
                    "- [Snapshot](./snapshot.yml)\n- [Outside](../secret.png)"
                ),
            }

    result = SimpleNamespace(content=[Block()], structuredContent=None, isError=False)
    config = MCPServerConfig.from_mapping({
        "name": "playwright",
        "command": "npx",
        "artifact_roots": ["playwright"],
        "artifact_extensions": ["png"],
        "max_artifact_bytes": 1024,
    })

    normalized = _normalize_call_result(result, config, work_dir=tmp_path)

    resources = [block for block in normalized.content if block.type == "resource"]
    assert len(resources) == 1
    assert resources[0].uri == screenshot.resolve().as_uri()
    assert resources[0].mime_type == "image/png"
    assert resources[0].metadata == {"filename": "example.png", "truncated": False}


def test_mcp_text_link_promotion_rejects_symlinks_and_marks_oversize(tmp_path):
    from luna_agent.mcp.connection import _normalize_call_result

    output = tmp_path / "playwright"
    output.mkdir()
    large = output / "large.png"
    large.write_bytes(b"too-large")
    linked = output / "linked.png"
    linked.symlink_to(large)

    class Block:
        def model_dump(self, **_kwargs):
            return {"type": "text", "text": "[Large](./large.png) [Link](./linked.png)"}

    result = SimpleNamespace(content=[Block()], structuredContent=None, isError=False)
    config = MCPServerConfig.from_mapping({
        "name": "playwright",
        "command": "npx",
        "artifact_roots": ["playwright"],
        "artifact_extensions": [".png"],
        "max_artifact_bytes": 4,
    })

    normalized = _normalize_call_result(result, config, work_dir=tmp_path)

    resources = [block for block in normalized.content if block.type == "resource"]
    assert len(resources) == 1
    assert resources[0].metadata["truncated"] is True


@pytest.mark.asyncio
async def test_mcp_tool_count_limit_is_enforced():
    class Session:
        async def list_tools(self, cursor=None):
            tools = [
                SimpleNamespace(name="one", description="", inputSchema={}),
                SimpleNamespace(name="two", description="", inputSchema={}),
            ]
            return SimpleNamespace(tools=tools, nextCursor=None)

    connection = SDKMCPConnection(
        MCPServerConfig(name="mock", command="python", max_tools=1)
    )
    connection._session = Session()

    with pytest.raises(ValueError, match="tool count"):
        await connection.list_tools()
