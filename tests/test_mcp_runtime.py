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
        self.list_error = None
        self.closed = False

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        return MCPServerInfo(name=self.config.name, version="1.0", protocol_version="test")

    async def list_tools(self):
        if self.list_error:
            raise self.list_error
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


class BlockingConnection(FakeConnection):
    def __init__(self, config, callback, gate: asyncio.Event):
        super().__init__(config, callback, tools=[tool("late")])
        self.gate = gate
        self.connect_started = asyncio.Event()

    async def connect(self):
        self.connect_started.set()
        await self.gate.wait()
        return await super().connect()


class BlockingCallConnection(FakeConnection):
    def __init__(self, config, callback):
        super().__init__(config, callback, tools=[tool("slow")])
        self.call_started = asyncio.Event()
        self.call_gate = asyncio.Event()

    async def call_tool(self, name, arguments):
        self.call_started.set()
        await self.call_gate.wait()
        return MCPCallResult(text="late")


async def wait_until(predicate, timeout: float = 1.0):
    end = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= end:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_runtime_uses_isolated_server_work_dir(tmp_path):
    runtime = MCPServerRuntime(
        MCPServerConfig.from_mapping({
            "name": "browser",
            "command": "python",
            "work_dir": "playwright",
        }),
        connection_factory=lambda config, callback: FakeConnection(config, callback),
        work_dir=tmp_path / "mcp",
    )
    try:
        await runtime.start()
        await runtime.wait_initial_attempt()
        assert runtime._work_dir == (tmp_path / "mcp" / "playwright").resolve()
        assert runtime._work_dir.is_dir()
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_start_does_not_wait_for_initial_connection():
    gate = asyncio.Event()
    created = []

    def factory(config, callback):
        connection = BlockingConnection(config, callback, gate)
        created.append(connection)
        return connection

    runtime = MCPServerRuntime(
        MCPServerConfig(name="slow", command="python"),
        connection_factory=factory,
        health_interval_seconds=60,
    )
    try:
        assert await asyncio.wait_for(runtime.start(), timeout=0.1) == 0
        await wait_until(lambda: bool(created))
        await created[0].connect_started.wait()
        assert runtime.state == MCPRuntimeState.CONNECTING
        assert runtime.health_snapshot()["initial_attempt_done"] is False

        gate.set()
        assert await runtime.wait_initial_attempt() == 1
        assert runtime.ready is True
        assert runtime.health_snapshot()["initial_attempt_done"] is True
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_stop_cancels_initial_connection():
    gate = asyncio.Event()
    created = []

    def factory(config, callback):
        connection = BlockingConnection(config, callback, gate)
        created.append(connection)
        return connection

    runtime = MCPServerRuntime(
        MCPServerConfig(name="slow-stop", command="python"),
        connection_factory=factory,
        health_interval_seconds=60,
    )
    await runtime.start()
    await wait_until(lambda: bool(created))
    await created[0].connect_started.wait()

    await asyncio.wait_for(runtime.stop(), timeout=0.5)

    assert created[0].closed is True
    assert runtime.state == MCPRuntimeState.STOPPED
    assert runtime.health_snapshot()["initial_attempt_done"] is True


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
async def test_runtime_does_not_retry_configuration_errors():
    attempts = 0

    def factory(config, callback):
        nonlocal attempts
        attempts += 1
        return FakeConnection(config, callback, connect_error=ValueError("missing header env"))

    runtime = MCPServerRuntime(
        MCPServerConfig(name="invalid", command="python"),
        connection_factory=factory,
        reconnect_delays=(0.01,),
        jitter=lambda: 0,
    )
    try:
        await runtime.start()
        await asyncio.sleep(0.03)

        assert runtime.state == MCPRuntimeState.FAILED
        assert attempts == 1
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
        assert await runtime.start() == 0
        assert await runtime.wait_initial_attempt() == 1
        connection = created[0]
        connection.tools = [tool("new", "updated")]
        await connection.notify_tools_changed()
        await connection.notify_tools_changed()
        await wait_until(lambda: "mcp__dynamic__new" in runtime.registered_names)

        assert "mcp__dynamic__old" not in tool_registry.all_names
        assert runtime.health_snapshot()["tool_refresh_count"] == 1
        assert runtime.health_snapshot()["notification_count"] == 2
        assert tool_registry.get("mcp__dynamic__new").description.endswith("updated")
    finally:
        await runtime.stop()

    assert "mcp__dynamic__new" not in tool_registry.all_names


@pytest.mark.asyncio
async def test_runtime_keeps_last_tool_snapshot_when_refresh_fails():
    created = []

    def factory(config, callback):
        connection = FakeConnection(config, callback, tools=[tool("stable")])
        created.append(connection)
        return connection

    runtime = MCPServerRuntime(
        MCPServerConfig(name="degraded", command="python"),
        connection_factory=factory,
        health_interval_seconds=60,
        refresh_debounce_seconds=0,
    )
    try:
        await runtime.start()
        await runtime.wait_initial_attempt()
        connection = created[0]
        connection.list_error = RuntimeError("refresh failed")
        await connection.notify_tools_changed()
        await wait_until(lambda: runtime.state == MCPRuntimeState.DEGRADED)

        entry = tool_registry.get("mcp__degraded__stable")
        assert runtime.ready is True
        assert entry is not None
        assert entry.check_fn is not None and entry.check_fn() is True
        assert "refresh failed" in runtime.health_snapshot()["last_error"]
    finally:
        await runtime.stop()


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
        await runtime.wait_initial_attempt()
        result = await runtime.call_tool("fragile", {})
        entry = tool_registry.get("mcp__unstable__fragile")

        assert result.is_error is True
        assert result.metadata["reason"] == "temporarily_unavailable"
        assert entry is not None
        assert entry.check_fn is not None and entry.check_fn() is False
        catalog = {item["name"]: item for item in tool_registry.catalog()}
        assert "reconnecting" in catalog["mcp__unstable__fragile"]["unavailable_reason"]
        assert "lost" in catalog["mcp__unstable__fragile"]["unavailable_reason"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_cancelled_tool_call_reconnects_mcp_transport():
    created = []

    def factory(config, callback):
        connection = BlockingCallConnection(config, callback)
        created.append(connection)
        return connection

    runtime = MCPServerRuntime(
        MCPServerConfig(name="cancelled", command="python"),
        connection_factory=factory,
        reconnect_delays=(0.01,),
        health_interval_seconds=60,
        jitter=lambda: 0,
    )
    try:
        await runtime.start()
        await runtime.wait_initial_attempt()
        first = created[0]
        task = asyncio.create_task(runtime.call_tool("slow", {}))
        await first.call_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await wait_until(lambda: len(created) >= 2 and runtime.ready)
        assert first.closed is True
        assert "cancelled" in runtime.health_snapshot()["last_call_error"].lower()
    finally:
        await runtime.stop()
