"""Tests for MCP client subsystem: client, manager, bridge integration."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from personal_agent.mcp.client import MCPClient, MCPServerConfig, MCPToolInfo


# ── Mock MCP server (Python script) ─────────────────────

MOCK_SERVER_SCRIPT = r"""
import json, sys

def respond(id, result):
    sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":id,"result":result}) + "\n")
    sys.stdout.flush()

def error(id, code, msg):
    sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":id,"error":{"code":code,"message":msg}}) + "\n")
    sys.stdout.flush()

TOOLS = [
    {"name":"echo","description":"Echo a message","inputSchema":{"type":"object","properties":{"msg":{"type":"string"}},"required":["msg"]}},
    {"name":"add","description":"Add two numbers","inputSchema":{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]}},
]

while True:
    line = sys.stdin.readline()
    if not line:
        break
    req = json.loads(line.strip())
    method = req.get("method", "")
    rid = req.get("id")

    if method == "initialize":
        respond(rid, {"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"mock","version":"1.0"}})
    elif method == "notifications/initialized":
        pass  # notification, no response
    elif method == "tools/list":
        respond(rid, {"tools": TOOLS})
    elif method == "tools/call":
        params = req.get("params", {})
        tool_name = params.get("name", "")
        args = params.get("arguments", {})
        if tool_name == "echo":
            respond(rid, {"content":[{"type":"text","text":args.get("msg","")}]})
        elif tool_name == "add":
            result = args.get("a", 0) + args.get("b", 0)
            respond(rid, {"content":[{"type":"text","text":str(result)}]})
        else:
            error(rid, -32601, f"Unknown tool: {tool_name}")
    elif method == "bad/method":
        error(rid, -32601, "Method not found")
    else:
        error(rid, -32601, f"Unknown method: {method}")
"""


@pytest.fixture
def mock_server_script(tmp_path: Path) -> Path:
    """Write the mock MCP server script to a temp file."""
    script = tmp_path / "mock_mcp_server.py"
    script.write_text(MOCK_SERVER_SCRIPT, encoding="utf-8")
    return script


def make_config(mock_server_script: Path) -> MCPServerConfig:
    return MCPServerConfig(
        name="mock",
        command=sys.executable,
        args=["-u", str(mock_server_script)],  # -u = unbuffered
    )


def test_http_mcp_tools_declare_server_network_resource():
    from personal_agent.mcp.models import MCPCallResult, MCPToolSpec
    from personal_agent.mcp.registrar import MCPToolRegistrar
    from personal_agent.tools.registry import tool_registry

    async def call_tool(_name, _arguments):
        return MCPCallResult()

    registrar = MCPToolRegistrar(
        "github",
        call_tool,
        server_url="https://mcp.github.example/api",
    )
    registrar.sync([MCPToolSpec(name="issues")])
    try:
        entry = tool_registry.get("mcp__github__issues")
        resources = entry.resource_resolver({})
    finally:
        registrar.unregister_all()

    assert len(resources) == 1
    assert resources[0].resource == "https://mcp.github.example:443"
    assert resources[0].access == "connect"


# ── MCPClient tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_client_connect_and_discover(mock_server_script: Path):
    """Connect to mock server and discover tools."""
    client = MCPClient(make_config(mock_server_script))
    try:
        tools = await client.connect()
        assert len(tools) == 2
        assert tools[0].name == "echo"
        assert tools[1].name == "add"
        assert "Echo" in tools[0].description
        assert client.connected is True
        health = client.health_snapshot()
        assert health["connected"] is True
        assert health["pid"] is None  # official SDK does not expose its child process handle
        assert health["tool_count"] == 2
        assert health["server_name"] == "mock"
        assert health["last_error"] == ""
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_client_call_tool_echo(mock_server_script: Path):
    """Call the echo tool on the mock server."""
    client = MCPClient(make_config(mock_server_script))
    try:
        await client.connect()
        result = await client.call_tool("echo", {"msg": "hello world"})
        assert result == "hello world"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_client_call_tool_add(mock_server_script: Path):
    """Call the add tool on the mock server."""
    client = MCPClient(make_config(mock_server_script))
    try:
        await client.connect()
        result = await client.call_tool("add", {"a": 3, "b": 4})
        assert result == "7"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_client_call_unknown_tool(mock_server_script: Path):
    """Calling an unknown tool should raise an error."""
    client = MCPClient(make_config(mock_server_script))
    try:
        await client.connect()
        with pytest.raises(RuntimeError, match="Unknown tool"):
            await client.call_tool("nonexistent", {})
        assert "Unknown tool" in client.health_snapshot()["last_call_error"]
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_client_disconnect(mock_server_script: Path):
    """Disconnect should clean up the subprocess."""
    client = MCPClient(make_config(mock_server_script))
    await client.connect()
    assert client.connected is True

    await client.disconnect()
    assert client.connected is False
    assert client._process is None
    assert len(client.tools) == 0
    health = client.health_snapshot()
    assert health["connected"] is False
    assert health["pid"] is None
    assert health["tool_count"] == 0
    assert health["last_disconnected_at"]


@pytest.mark.asyncio
async def test_client_double_connect(mock_server_script: Path):
    """Second connect should be a no-op, returning cached tools."""
    client = MCPClient(make_config(mock_server_script))
    try:
        tools1 = await client.connect()
        tools2 = await client.connect()
        assert len(tools1) == len(tools2)
        # Same client object returned cached tools (not re-spawned)
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_client_bad_command():
    """Non-existent command should return empty tools, not crash."""
    client = MCPClient(MCPServerConfig(
        name="bad", command="nonexistent_command_xyz_123", args=[]
    ))
    tools = await client.connect()
    assert tools == []
    assert client.connected is False
    assert "command not found" in client.health_snapshot()["last_error"]


@pytest.mark.asyncio
async def test_client_call_without_connect():
    """Calling tool before connect should return error string."""
    client = MCPClient(MCPServerConfig(name="test", command="echo", args=[]))
    result = await client.call_tool("echo", {"msg": "hi"})
    assert "not connected" in result.lower()
    assert "not connected" in client.health_snapshot()["last_call_error"].lower()


@pytest.mark.asyncio
async def test_client_stderr_tail_is_captured(tmp_path: Path):
    """Recent stderr lines should be available in the health snapshot."""
    script = tmp_path / "stderr_mcp_server.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write('mcp startup warning\\n')\n"
        "sys.stderr.flush()\n"
        + MOCK_SERVER_SCRIPT,
        encoding="utf-8",
    )
    client = MCPClient(make_config(script))
    try:
        await client.connect()
        for _ in range(20):
            if client.health_snapshot()["stderr_tail"]:
                break
            await asyncio.sleep(0.05)
        assert any(
            "mcp startup warning" in line
            for line in client.health_snapshot()["stderr_tail"]
        )
    finally:
        await client.disconnect()


# ── MCPManager tests ────────────────────────────────────


@pytest.mark.asyncio
async def test_manager_start_stop(mock_server_script: Path):
    """Manager should connect, register tools, and clean up."""
    from personal_agent.mcp.manager import MCPManager
    from personal_agent.tools.registry import tool_registry

    server_cfg = {
        "name": "mock",
        "command": sys.executable,
        "args": ["-u", str(mock_server_script)],
        "enabled": True,
    }

    manager = MCPManager([server_cfg])
    count_before = len(tool_registry.all_names)

    try:
        count = await manager.start()
        assert count == 2
        assert manager.total_tools == 2
        assert "mock" in manager.client_names
        health = manager.health_snapshot()
        assert health["running"] is True
        assert health["configured_count"] == 1
        assert health["connected_count"] == 1
        assert health["total_tools"] == 2
        assert sorted(health["registered_tools"]) == ["mcp__mock__add", "mcp__mock__echo"]

        # Tools should be registered
        assert "mcp__mock__echo" in tool_registry.all_names
        assert "mcp__mock__add" in tool_registry.all_names

        # Tools should have correct properties
        echo_entry = tool_registry.get("mcp__mock__echo")
        assert echo_entry is not None
        assert echo_entry.toolset == "mcp"
        assert echo_entry.is_parallel_safe is True

    finally:
        await manager.stop()
        # Tools should be unregistered
        assert "mcp__mock__echo" not in tool_registry.all_names
        assert "mcp__mock__add" not in tool_registry.all_names
        after_count = len(tool_registry.all_names)
        assert after_count == count_before + 0  # no leftover MCP tools
        assert manager.health_snapshot()["running"] is False
        assert manager.health_snapshot()["registered_tools"] == []


@pytest.mark.asyncio
async def test_manager_disabled_server():
    """Disabled server should be skipped."""
    from personal_agent.mcp.manager import MCPManager

    manager = MCPManager([{
        "name": "disabled_srv",
        "command": "echo",
        "args": [],
        "enabled": False,
    }])
    count = await manager.start()
    assert count == 0
    assert manager.total_tools == 0


@pytest.mark.asyncio
async def test_manager_no_servers():
    """Empty config should be a no-op."""
    from personal_agent.mcp.manager import MCPManager

    manager = MCPManager([])
    count = await manager.start()
    assert count == 0


def test_manager_rejects_duplicate_server_names():
    from personal_agent.mcp.manager import MCPManager

    with pytest.raises(ValueError, match="Duplicate MCP server"):
        MCPManager([
            {"name": "same", "command": "one"},
            {"name": "same", "command": "two"},
        ])


@pytest.mark.asyncio
async def test_manager_bad_server_doesnt_block_others(mock_server_script: Path):
    """One bad server shouldn't prevent good ones from connecting."""
    from personal_agent.mcp.manager import MCPManager
    from personal_agent.tools.registry import tool_registry

    configs = [
        {
            "name": "bad_one",
            "command": "nonexistent_cmd_abc_123",
            "args": [],
            "enabled": True,
        },
        {
            "name": "mock",
            "command": sys.executable,
            "args": ["-u", str(mock_server_script)],
            "enabled": True,
        },
    ]

    manager = MCPManager(configs)
    try:
        count = await manager.start()
        assert count == 2  # mock server's 2 tools
        assert "mock" in manager.client_names
        assert "bad_one" not in manager.client_names
        health = manager.health_snapshot()
        assert health["configured_count"] == 2
        assert health["connected_count"] == 1
        servers = {item["name"]: item for item in health["servers"]}
        assert servers["mock"]["connected"] is True
        assert servers["mock"]["tool_count"] == 2
        assert servers["bad_one"]["connected"] is False
        assert "command not found" in servers["bad_one"]["last_error"]
    finally:
        await manager.stop()


# ── Bridge integration: tool_search discovers MCP tools ─


@pytest.mark.asyncio
async def test_tool_search_discovers_mcp_tools(mock_server_script: Path):
    """tool_search should return MCP tools (deferrable)."""
    from personal_agent.mcp.manager import MCPManager
    from personal_agent.tools.registry import dispatch_tool_search

    manager = MCPManager([{
        "name": "mock",
        "command": sys.executable,
        "args": ["-u", str(mock_server_script)],
        "enabled": True,
    }])

    try:
        await manager.start()

        # Search for "echo" should find the MCP echo tool
        result = await dispatch_tool_search("echo")
        data = json.loads(result)
        hits = data.get("hits", [])

        echo_names = [h["name"] for h in hits]
        assert "mcp__mock__echo" in echo_names

        # Hit should include input_schema
        echo_hit = next(h for h in hits if h["name"] == "mcp__mock__echo")
        assert "input_schema" in echo_hit
        assert "msg" in str(echo_hit["input_schema"])

    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_mcp_tools_not_in_core_list():
    """MCP tools are NOT core — they must be discoverable via tool_search."""
    from personal_agent.tools.toolsets import is_core_tool, _CORE_TOOLS

    # MCP prefix tools are not in the core list
    assert "mcp__mock__echo" not in _CORE_TOOLS
    assert is_core_tool("mcp__mock__echo") is False
    assert is_core_tool("mcp__github__search_repos") is False


# ── MCP tool call through executor ──────────────────────


@pytest.mark.asyncio
async def test_exec_mcp_tool_through_pipeline(mock_server_script: Path):
    """An MCP tool should be callable through the executor pipeline."""
    from personal_agent.mcp.manager import MCPManager
    from personal_agent.security.session import SecurityStateStore
    from personal_agent.tools.executor import execute_tool_call_result, format_tool_result
    from types import SimpleNamespace

    manager = MCPManager([{
        "name": "mock",
        "command": sys.executable,
        "args": ["-u", str(mock_server_script)],
        "enabled": True,
    }])

    try:
        await manager.start()

        tc = {"name": "mcp__mock__echo", "input": {"msg": "from executor"}}
        settings = SimpleNamespace(
            execution_mode="ask-first",
            sandbox_roots=[mock_server_script.parent],
            permission_grant_ttl_minutes=60,
        )
        agent = SimpleNamespace(
            _security_context=SecurityStateStore(settings).context("mcp"),
            _security_grant_ttl_seconds=3600,
            _tool_calls_this_turn=0,
            _max_tool_calls_per_turn=20,
            _destructive_calls_this_turn=0,
            _max_destructive_per_turn=3,
            _interrupt_requested=False,
        )

        async def approve(_decision):
            return "always"

        result = format_tool_result(await execute_tool_call_result(tc, agent=agent, confirm=approve))
        assert result == "from executor"

    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_mcp_tool_search_integration(mock_server_script: Path):
    """End-to-end: search for MCP tool, then call it via executor."""
    from personal_agent.mcp.manager import MCPManager
    from personal_agent.tools.registry import dispatch_tool_search
    from personal_agent.security.session import SecurityStateStore
    from personal_agent.tools.executor import execute_tool_call_result, format_tool_result
    from types import SimpleNamespace

    manager = MCPManager([{
        "name": "mock",
        "command": sys.executable,
        "args": ["-u", str(mock_server_script)],
        "enabled": True,
    }])

    try:
        await manager.start()

        # 1. LLM searches for "add numbers"
        search_result = json.loads(await dispatch_tool_search("add numbers"))
        hits = search_result.get("hits", [])
        assert len(hits) > 0

        # 2. LLM gets the schema (simulated — tool_search already returns it)
        add_hit = next(h for h in hits if h["name"] == "mcp__mock__add")
        assert "input_schema" in add_hit
        assert "a" in str(add_hit["input_schema"])
        assert "b" in str(add_hit["input_schema"])

        # 3. LLM calls the tool directly
        tc = {"name": "mcp__mock__add", "input": {"a": 10, "b": 32}}
        settings = SimpleNamespace(
            execution_mode="ask-first",
            sandbox_roots=[mock_server_script.parent],
            permission_grant_ttl_minutes=60,
        )
        agent = SimpleNamespace(
            _security_context=SecurityStateStore(settings).context("mcp-search"),
            _security_grant_ttl_seconds=3600,
            _tool_calls_this_turn=0,
            _max_tool_calls_per_turn=20,
            _destructive_calls_this_turn=0,
            _max_destructive_per_turn=3,
            _interrupt_requested=False,
        )

        async def approve(_decision):
            return "always"

        result = format_tool_result(await execute_tool_call_result(tc, agent=agent, confirm=approve))
        assert result == "42"

    finally:
        await manager.stop()
