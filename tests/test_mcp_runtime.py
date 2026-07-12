from __future__ import annotations

import asyncio

import pytest

from personal_agent.mcp.models import (
    MCPCallResult,
    MCPRuntimeState,
    MCPServerConfig,
    MCPServerInfo,
    MCPToolSpec,
)
from personal_agent.mcp.runtime import MCPServerRuntime
from personal_agent.tools.registry import tool_registry


class FakeConnection:
    def __init__(self, config, callback, *, tools=None, connect_error=None, call_error=None):
        self.config = config
        self.callback = callback
        self.tools = list(tools or [])
        self.connect_error = connect_error
        self.call_error = call_error
        self.closed = False

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        return MCPServerInfo(name=self.config.name, version="1.0", protocol_version="test")

    async def list_tools(self):
        return list(self.tools)

    async def call_tool(self, name, arguments):
        if self.call_error:
            raise self.call_error
        return MCPCallResult(text=str(arguments.get("value", name)))

    async def ping(self):
        return None

    async def close(self):
        self.closed = True

    async def notify_tools_changed(self):
        value = self.callback("tools/list_changed")
        if asyncio.iscoroutine(value):
            await value


def tool(name: str, description: str = "test") -> MCPToolSpec:
    return MCPToolSpec(name=name, description=description, input_schema={"type": "object"})


async def wait_until(predicate, timeout: float = 1.0):
    end = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= end:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_runtime_recovers_after_initial_connection_failure():
    created = []
    outcomes = [FileNotFoundError("missing"), None]

    def factory(config, callback):
        connection = FakeConnection(
            config,
            callback,
            tools=[tool("echo")],
            connect_error=outcomes.pop(0),
        )
        created.append(connection)
        return connection

    runtime = MCPServerRuntime(
        MCPServerConfig(name="recover", command="missing"),
        connection_factory=factory,
        reconnect_delays=(0.01,),
        health_interval_seconds=60,
        jitter=lambda: 0,
    )
    try:
        assert await runtime.start() == 0
        await wait_until(lambda: runtime.ready)

        assert runtime.state == MCPRuntimeState.READY
        assert runtime.registered_names == {"mcp__recover__echo"}
        assert runtime.health_snapshot()["reconnect_attempts"] == 1
        assert created[0].closed is True
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_refreshes_tool_snapshot_from_notification():
    created = []

    def factory(config, callback):
        connection = FakeConnection(config, callback, tools=[tool("old")])
        created.append(connection)
        return connection

    runtime = MCPServerRuntime(
        MCPServerConfig(name="dynamic", command="python"),
        connection_factory=factory,
        health_interval_seconds=60,
        refresh_debounce_seconds=0,
    )
    try:
        assert await runtime.start() == 1
        connection = created[0]
        connection.tools = [tool("new")]
        await connection.notify_tools_changed()
        await wait_until(lambda: "mcp__dynamic__new" in runtime.registered_names)

        assert "mcp__dynamic__old" not in tool_registry.all_names
        assert runtime.health_snapshot()["tool_refresh_count"] == 1
        assert runtime.health_snapshot()["notification_count"] == 1
    finally:
        await runtime.stop()

    assert "mcp__dynamic__new" not in tool_registry.all_names


@pytest.mark.asyncio
async def test_runtime_keeps_tool_registered_but_unavailable_during_recovery():
    created = []

    def factory(config, callback):
        connection = FakeConnection(
            config,
            callback,
            tools=[tool("fragile")],
            call_error=ConnectionError("lost"),
        )
        created.append(connection)
        return connection

    runtime = MCPServerRuntime(
        MCPServerConfig(name="unstable", command="python"),
        connection_factory=factory,
        reconnect_delays=(60,),
        health_interval_seconds=60,
        jitter=lambda: 0,
    )
    try:
        await runtime.start()
        result = await runtime.call_tool("fragile", {})
        entry = tool_registry.get("mcp__unstable__fragile")

        assert result.is_error is True
        assert result.metadata["reason"] == "temporarily_unavailable"
        assert entry is not None
        assert entry.check_fn is not None and entry.check_fn() is False
    finally:
        await runtime.stop()
