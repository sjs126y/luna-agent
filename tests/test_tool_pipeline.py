"""Targeted tests for the new tool execution pipeline: scope gate, shell whitelist,
file_write safety checks, bridge destructive blocking, checkpoint, audit."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# ── Shell command whitelist ────────────────────────────


def test_shell_allowed_commands():
    from personal_agent.plugins.builtin.tools.builtin.bash import _check_command

    # Safe commands should pass
    for cmd in ["ls -la", "cat file.txt", "grep pattern file", "git status",
                "python --version", "echo hello", "whoami", "pwd", "date"]:
        assert _check_command(cmd) is None, f"'{cmd}' should be allowed"

    # Command chaining (&&, ;, |) should be caught by dangerous patterns
    # Known: whoami is whitelisted, and '&& ls' doesn't match current dangerous patterns
    # This is a real security gap — see test_shell_command_chaining_bypass
    for cmd in ["nmap -sP 192.168.1.1", "nc -lvp 4444", "evil_command"]:
        result = _check_command(cmd)
        assert result is not None, f"'{cmd}' should be blocked"


def test_shell_command_chaining_bypass():
    """Command chaining (&& || | ;) is now blocked — one command per call."""
    from personal_agent.plugins.builtin.tools.builtin.bash import _check_command

    # All chain operators are blocked
    assert _check_command("whoami && ls") is not None     # blocked
    assert _check_command("whoami && rm -rf /") is not None  # blocked
    assert _check_command("ls || echo fail") is not None  # blocked
    assert _check_command("cat file | grep x") is not None  # blocked
    assert _check_command("echo hello; ls") is not None   # blocked

    # Single commands still work
    assert _check_command("whoami") is None               # allowed
    assert _check_command("ls -la") is None               # allowed


def test_shell_network_blocked():
    from personal_agent.plugins.builtin.tools.builtin.bash import _check_command, _allow_network

    assert _allow_network is False  # default should be false

    for cmd in ["curl http://example.com", "wget http://example.com",
                "pip install requests", "npm install lodash"]:
        result = _check_command(cmd)
        assert result is not None, f"'{cmd}' should be blocked (network)"
        assert "network" in result.lower()


def test_shell_dangerous_patterns():
    from personal_agent.plugins.builtin.tools.builtin.bash import _check_command

    dangerous = [
        "rm -rf /",
        "dd if=/dev/zero of=/dev/sda",
        "chmod 777 /etc/passwd",
    ]
    for cmd in dangerous:
        result = _check_command(cmd)
        assert result is not None, f"'{cmd}' should match dangerous pattern"


# ── File write safety checks ───────────────────────────


def test_file_write_extension_whitelist():
    from personal_agent.plugins.builtin.tools.builtin.file_write import _check_extension

    # Allowed extensions
    for ext in [".txt", ".md", ".json", ".py", ".js", ".html", ".css", ".csv",
                ".yaml", ".yml", ".toml", ".log", ".xml", ".sh", ".bat"]:
        assert _check_extension(f"test{ext}") is None, f"'{ext}' should be allowed"

    # Blocked extensions
    for ext in [".exe", ".dll", ".so", ".bin", ".com", ".msi", ".scr"]:
        result = _check_extension(f"test{ext}")
        assert result is not None, f"'{ext}' should be blocked"


def test_file_write_max_size():
    from personal_agent.plugins.builtin.tools.builtin.file_write import _MAX_WRITE_BYTES

    assert _MAX_WRITE_BYTES == 100_000  # default 100KB


@pytest.mark.asyncio
async def test_file_write_large_content():
    from personal_agent.plugins.builtin.tools.builtin.file_write import _file_write

    large = "x" * 200_000
    result = await _file_write("large.txt", large)
    assert "too large" in result.lower()


@pytest.mark.asyncio
async def test_file_write_path_traversal():
    from personal_agent.tools.sandbox import init_sandbox
    from personal_agent.plugins.builtin.tools.builtin.file_write import _file_write

    init_sandbox([Path("./data")], ["**/.env", "**/.git/**", "**/.ssh/**"])
    result = await _file_write("../../../etc/passwd", "hello")
    assert "outside" in result.lower()


# ── Bridge tool_call blocking destructive tools ────────


@pytest.mark.asyncio
async def test_bridge_tool_call_blocks_destructive():
    import personal_agent.plugins.builtin.tools.builtin.file_write  # noqa: F401

    from personal_agent.plugins.builtin.tools.bridge.bridge import _tool_call

    # file_write is destructive — should be blocked via tool_call
    result = await _tool_call("write", {"path": "test.txt", "content": "hello"})
    assert "destructive" in result.lower() or "cannot be called" in result.lower()


@pytest.mark.asyncio
async def test_bridge_tool_call_allows_safe():
    from personal_agent.plugins.builtin.tools.bridge.bridge import _tool_call

    # tool_search is safe — should work
    result = await _tool_call("tool_search", {"query": "search"})
    assert "destructive" not in result.lower()


# ── Executor scope gate ────────────────────────────────


class MockAgent:
    def __init__(self):
        self._destructive_allowed: set[str] = set()
        self._tool_calls_this_turn: int = 0
        self._max_tool_calls_per_turn: int = 20
        self._execution_policy = None


def test_tool_entry_permission_category_defaults_to_default():
    from personal_agent.tools.entry import ToolEntry

    entry = ToolEntry(
        name="demo",
        description="Demo",
        schema={},
        handler=lambda **kw: "ok",
    )

    assert entry.permission_category == "default"


def test_execution_guard_uses_tool_permission_metadata_before_fallback():
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.execution_guard import evaluate_execution_guards

    agent = MockAgent()
    agent._execution_policy = ExecutionPolicy(
        mode="standard",
        permissions={"default": "allow", "network": "ask"},
    )
    tc = {"name": "custom_plugin_tool", "input": {}}
    entry = ToolEntry(
        name="custom_plugin_tool",
        description="Custom",
        schema={},
        handler=lambda **kw: "ok",
        permission_category="network",
    )

    decision = evaluate_execution_guards(tc, entry, agent)

    assert decision.allowed is False
    assert decision.stage == "permission"
    assert decision.category == "network"
    assert decision.reason_code == "permission_required"


def test_execution_guard_fallback_category_still_supports_legacy_tools():
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.execution_guard import evaluate_execution_guards

    agent = MockAgent()
    agent._execution_policy = ExecutionPolicy(
        mode="standard",
        permissions={"default": "allow", "background": "ask"},
    )
    tc = {"name": "process_start", "input": {"command": "python -c \"print(1)\""}}
    entry = ToolEntry(
        name="process_start",
        description="Legacy process",
        schema={},
        handler=lambda **kw: "ok",
    )

    decision = evaluate_execution_guards(tc, entry, agent)

    assert decision.allowed is False
    assert decision.category == "background"
    assert decision.required_allow == "background"


def test_scope_gate_destructive_blocked_by_default():
    from personal_agent.tools.executor import _scope_gate
    from personal_agent.tools.entry import ToolEntry

    agent = MockAgent()
    tc = {"name": "write", "input": {"path": "x.txt", "content": "hi"}}
    entry = ToolEntry(
        name="write",
        description="Write file",
        schema={},
        handler=lambda **kw: "ok",
        toolset="builtin",
        is_destructive=True,
    )

    result = _scope_gate(tc, entry, agent)
    assert result is not None
    assert "authorization" in result.lower() or "allow" in result.lower()


def test_scope_gate_destructive_allowed_after_allow():
    from personal_agent.tools.executor import _scope_gate
    from personal_agent.tools.entry import ToolEntry

    agent = MockAgent()
    agent._destructive_allowed.add("write")

    tc = {"name": "write", "input": {"path": "x.txt", "content": "hi"}}
    entry = ToolEntry(
        name="write",
        description="Write",
        schema={},
        handler=lambda **kw: "ok",
        toolset="builtin",
        is_destructive=True,
    )

    result = _scope_gate(tc, entry, agent)
    assert result is None  # allowed now


def test_scope_gate_allow_all():
    from personal_agent.tools.executor import _scope_gate
    from personal_agent.tools.entry import ToolEntry

    agent = MockAgent()
    agent._destructive_allowed.add("all")

    tc = {"name": "write", "input": {"path": "x.txt", "content": "hi"}}
    entry = ToolEntry(
        name="write",
        description="Write",
        schema={},
        handler=lambda **kw: "ok",
        toolset="builtin",
        is_destructive=True,
    )

    result = _scope_gate(tc, entry, agent)
    assert result is None  # "all" bypasses everything


def test_scope_gate_non_destructive_passes():
    from personal_agent.tools.executor import _scope_gate
    from personal_agent.tools.entry import ToolEntry

    agent = MockAgent()
    tc = {"name": "calculator", "input": {"expression": "2+2"}}
    entry = ToolEntry(
        name="calculator",
        description="Calculate",
        schema={},
        handler=lambda **kw: "4",
        toolset="utility",
        is_destructive=False,
    )

    result = _scope_gate(tc, entry, agent)
    assert result is None  # safe tool, always passes


def test_scope_gate_tool_call_quota():
    from personal_agent.tools.executor import _scope_gate
    from personal_agent.tools.entry import ToolEntry

    agent = MockAgent()
    agent._tool_calls_this_turn = 20  # already at limit
    agent._max_tool_calls_per_turn = 20

    tc = {"name": "calculator", "input": {"expression": "1+1"}}
    entry = ToolEntry(
        name="calculator",
        description="Calc",
        schema={},
        handler=lambda **kw: "2",
        toolset="utility",
        is_destructive=False,
    )

    result = _scope_gate(tc, entry, agent)
    assert result is not None
    assert "limit" in result.lower()


def test_scope_gate_guarded_denies_bash_even_with_allow():
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.tools.executor import _scope_gate
    from personal_agent.tools.entry import ToolEntry

    agent = MockAgent()
    agent._destructive_allowed.add("bash")
    agent._execution_policy = ExecutionPolicy(
        mode="guarded",
        permissions={"default": "deny", "bash": "deny"},
    )
    tc = {"name": "bash", "input": {"command": "ls"}}
    entry = ToolEntry(
        name="bash",
        description="Bash",
        schema={},
        handler=lambda **kw: "ok",
        toolset="builtin",
    )

    result = _scope_gate(tc, entry, agent)
    assert result is not None
    assert "denied by execution mode" in result


def test_scope_gate_standard_bash_requires_allow():
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.tools.executor import _scope_gate
    from personal_agent.tools.entry import ToolEntry

    agent = MockAgent()
    agent._execution_policy = ExecutionPolicy(
        mode="standard",
        permissions={"default": "ask", "bash": "ask"},
    )
    tc = {"name": "bash", "input": {"command": "ls"}}
    entry = ToolEntry(
        name="bash",
        description="Bash",
        schema={},
        handler=lambda **kw: "ok",
        toolset="builtin",
    )

    blocked = _scope_gate(tc, entry, agent)
    assert blocked is not None
    agent._destructive_allowed.add("bash")
    assert _scope_gate(tc, entry, agent) is None


def test_scope_gate_trusted_allows_workspace_write_policy():
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.tools.executor import _scope_gate
    from personal_agent.tools.entry import ToolEntry

    agent = MockAgent()
    agent._execution_policy = ExecutionPolicy(
        mode="trusted",
        permissions={"default": "ask", "write": "allow"},
    )
    tc = {"name": "write", "input": {"path": "x.txt", "content": "hi"}}
    entry = ToolEntry(
        name="write",
        description="Write",
        schema={},
        handler=lambda **kw: "ok",
        toolset="builtin",
        is_destructive=True,
    )

    assert _scope_gate(tc, entry, agent) is None


def test_scope_gate_standard_background_requires_allow():
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.tools.executor import _scope_gate
    from personal_agent.tools.entry import ToolEntry

    agent = MockAgent()
    agent._execution_policy = ExecutionPolicy(
        mode="standard",
        permissions={"default": "ask", "background": "ask"},
    )
    tc = {"name": "process_start", "input": {"command": "python -c \"print(1)\""}}
    entry = ToolEntry(
        name="process_start",
        description="Start process",
        schema={},
        handler=lambda **kw: "ok",
        toolset="builtin",
        permission_category="background",
    )

    blocked = _scope_gate(tc, entry, agent)
    assert blocked is not None
    assert "background" in blocked
    agent._destructive_allowed.add("background")
    assert _scope_gate(tc, entry, agent) is None


def test_scope_gate_policy_override_allows_background():
    from types import SimpleNamespace

    from personal_agent.execution import resolve_execution_policy
    from personal_agent.tools.executor import _scope_gate
    from personal_agent.tools.entry import ToolEntry

    agent = MockAgent()
    agent._execution_policy = resolve_execution_policy(SimpleNamespace(
        execution_mode="standard",
        bash_allow_network=False,
        execution_policy_overrides={"background": "allow"},
    ))
    tc = {"name": "process_start", "input": {"command": "python -c \"print(1)\""}}
    entry = ToolEntry(
        name="process_start",
        description="Start process",
        schema={},
        handler=lambda **kw: "ok",
        toolset="builtin",
        permission_category="background",
    )

    assert _scope_gate(tc, entry, agent) is None


def test_scope_gate_policy_override_can_tighten_trusted_bash():
    from types import SimpleNamespace

    from personal_agent.execution import resolve_execution_policy
    from personal_agent.tools.executor import _scope_gate
    from personal_agent.tools.entry import ToolEntry

    agent = MockAgent()
    agent._execution_policy = resolve_execution_policy(SimpleNamespace(
        execution_mode="trusted",
        bash_allow_network=False,
        execution_policy_overrides={"tool_permissions": {"bash": "ask"}},
    ))
    tc = {"name": "bash", "input": {"command": "ls"}}
    entry = ToolEntry(
        name="bash",
        description="Bash",
        schema={},
        handler=lambda **kw: "ok",
        toolset="builtin",
        permission_category="bash",
    )

    blocked = _scope_gate(tc, entry, agent)
    assert blocked is not None
    assert "/allow bash" in blocked


def test_scope_gate_guarded_denies_background():
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.tools.executor import _scope_gate
    from personal_agent.tools.entry import ToolEntry

    agent = MockAgent()
    agent._destructive_allowed.add("background")
    agent._execution_policy = ExecutionPolicy(
        mode="guarded",
        permissions={"default": "deny", "background": "deny"},
    )
    tc = {"name": "process_start", "input": {"command": "python -c \"print(1)\""}}
    entry = ToolEntry(
        name="process_start",
        description="Start process",
        schema={},
        handler=lambda **kw: "ok",
        toolset="builtin",
        permission_category="background",
    )

    result = _scope_gate(tc, entry, agent)
    assert result is not None
    assert "denied by execution mode" in result


def test_scope_gate_process_clear_uses_background_permission():
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.tools.executor import _scope_gate
    from personal_agent.tools.entry import ToolEntry

    agent = MockAgent()
    agent._execution_policy = ExecutionPolicy(
        mode="standard",
        permissions={"default": "allow", "background": "ask"},
    )
    tc = {"name": "process_clear", "input": {"status": "finished"}}
    entry = ToolEntry(
        name="process_clear",
        description="Clear processes",
        schema={},
        handler=lambda **kw: "ok",
        toolset="builtin",
        permission_category="background",
    )

    blocked = _scope_gate(tc, entry, agent)
    assert blocked is not None
    assert "background" in blocked


@pytest.mark.asyncio
async def test_process_start_precheck_runs_before_background_allow(tmp_path: Path):
    import personal_agent.plugins.builtin.tools.builtin.process_tool  # noqa: F401
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.plugins.builtin.tools.builtin.bash import set_allow_network, set_work_dir
    from personal_agent.tools.executor import execute_tool_call_result
    from personal_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], ["**/.env"])
    set_work_dir(tmp_path)
    set_allow_network(False)

    agent = MockAgent()
    agent._destructive_allowed.add("background")
    agent._execution_policy = ExecutionPolicy(
        mode="standard",
        permissions={"default": "ask", "background": "ask"},
    )

    hard_blacklist = await execute_tool_call_result(
        {"id": "p1", "name": "process_start", "input": {"command": "rm -rf /", "cwd": str(tmp_path)}},
        agent=agent,
    )
    blocked_secret = await execute_tool_call_result(
        {"id": "p2", "name": "process_start", "input": {"command": "cat .env", "cwd": str(tmp_path)}},
        agent=agent,
    )
    blocked_network = await execute_tool_call_result(
        {
            "id": "p3",
            "name": "process_start",
            "input": {"command": "curl https://example.com", "cwd": str(tmp_path)},
        },
        agent=agent,
    )

    assert hard_blacklist.status == "denied"
    assert hard_blacklist.category == "precheck"
    assert "hard blacklist" in hard_blacklist.error.lower()
    assert blocked_secret.status == "denied"
    assert blocked_secret.category == "precheck"
    assert "sandbox" in blocked_secret.error.lower()
    assert blocked_network.status == "denied"
    assert blocked_network.category == "precheck"
    assert "network" in blocked_network.error.lower()


@pytest.mark.asyncio
async def test_tool_end_event_includes_guard_metadata_for_denial(tmp_path: Path):
    import personal_agent.plugins.builtin.tools.builtin.process_tool  # noqa: F401
    from personal_agent.conversation.events import EventRecorder
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.plugins.builtin.tools.builtin.bash import set_work_dir
    from personal_agent.tools.executor import execute_tool_call_result
    from personal_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])
    set_work_dir(tmp_path)

    agent = MockAgent()
    agent._execution_policy = ExecutionPolicy(
        mode="standard",
        permissions={"default": "allow", "background": "ask"},
    )
    recorder = EventRecorder()

    result = await execute_tool_call_result(
        {
            "id": "p1",
            "name": "process_start",
            "input": {
                "command": "python -c \"print(1)\"",
                "cwd": str(tmp_path),
                "timeout_seconds": 30,
                "label": "unit test process",
            },
        },
        agent=agent,
        event_sink=recorder,
    )

    decision_event = recorder.events[-2]
    event = recorder.events[-1]
    assert result.status == "denied"
    assert [item.type for item in recorder.events] == ["tool_start", "tool_decision", "tool_end"]
    assert decision_event.type == "tool_decision"
    assert decision_event.data["allowed"] is False
    assert decision_event.data["stage"] == "permission"
    assert decision_event.data["status"] == "denied"
    assert decision_event.data["reason_code"] == "permission_required"
    assert event.type == "tool_end"
    assert event.data["guard_stage"] == "permission"
    assert event.data["guard_reason_code"] == "permission_required"
    assert event.data["permission_category"] == "background"
    assert event.data["permission_decision"] == "ask"
    assert event.data["required_allow"] == "background"
    assert event.data["execution_mode"] == "standard"
    assert event.data["display_name"] == "Start background process"
    assert event.data["execution_mode_label"] == "Ask First"
    assert event.data["risk_level"] == "medium"
    assert event.data["default_action"] == "none"
    assert event.data["available_actions"] == ["allow_once", "allow_always", "deny"]
    assert event.data["command_preview"] == 'python -c "print(1)"'
    assert event.data["input_preview"] == 'python -c "print(1)"'
    assert event.data["cwd"] == str(tmp_path)
    assert event.data["timeout_seconds"] == 30.0
    assert event.data["process_label"] == "unit test process"
    assert decision_event.data["cwd"] == str(tmp_path)
    assert decision_event.data["timeout_seconds"] == 30.0
    assert decision_event.data["process_label"] == "unit test process"


@pytest.mark.asyncio
async def test_tool_decision_event_includes_confirmation_display_metadata(tmp_path: Path):
    from personal_agent.conversation.events import EventRecorder
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.executor import execute_tool_call_result
    from personal_agent.tools.registry import tool_registry
    from personal_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])

    async def handler(**kwargs):
        return "ok"

    entry = ToolEntry(
        name="custom_write_preview",
        description="Custom write",
        schema={},
        handler=handler,
        toolset="test",
        is_destructive=True,
        permission_category="write",
    )
    previous = tool_registry.get("custom_write_preview")
    tool_registry.register(entry)
    agent = MockAgent()
    agent._execution_policy = ExecutionPolicy(
        mode="standard",
        permissions={"default": "allow", "write": "ask"},
    )
    recorder = EventRecorder()
    seen_decisions = []

    async def deny_confirm(decision):
        seen_decisions.append(decision)
        return "deny"

    try:
        result = await execute_tool_call_result(
            {
                "id": "w1",
                "name": "custom_write_preview",
                "input": {"path": "demo.txt", "content": "secret token sk-proj-abcdefghijklmnopqrstuvwxyz"},
            },
            agent=agent,
            event_sink=recorder,
            confirm=deny_confirm,
        )
    finally:
        if previous is not None:
            tool_registry.register(previous)
        else:
            tool_registry.unregister("custom_write_preview")

    assert result.status == "denied"
    assert len(seen_decisions) == 1
    decision = seen_decisions[0]
    assert decision.tool_use_id == "w1"
    assert decision.display_name == "Custom Write Preview"
    assert decision.execution_mode_label == "Ask First"
    assert decision.risk_level == "medium"
    assert decision.default_action == "allow"
    assert decision.available_actions == ("allow_once", "allow_always", "deny")
    assert decision.affected_paths == ("demo.txt",)
    assert decision.input_preview == "demo.txt"
    assert "abcdefghijklmnopqrstuvwxyz" not in decision.input_summary

    decision_event = next(event for event in recorder.events if event.type == "tool_decision")
    end_event = recorder.events[-1]
    assert decision_event.data["display_name"] == "Custom Write Preview"
    assert decision_event.data["execution_mode_label"] == "Ask First"
    assert decision_event.data["default_action"] == "allow"
    assert decision_event.data["available_actions"] == ["allow_once", "allow_always", "deny"]
    assert decision_event.data["affected_paths"] == ["demo.txt"]
    assert decision_event.data["input_preview"] == "demo.txt"
    assert "abcdefghijklmnopqrstuvwxyz" not in decision_event.data["input_summary"]
    assert end_event.type == "tool_end"
    assert end_event.data["status"] == "denied"
    assert end_event.data["category"] == "authorization"
    assert end_event.data["display_name"] == "Custom Write Preview"
    assert end_event.data["affected_paths"] == ["demo.txt"]


def test_tool_decision_display_includes_network_preview_metadata():
    from personal_agent.tools.execution_guard import GuardDecision, tool_decision_from_guard

    decision = tool_decision_from_guard(
        {
            "id": "net1",
            "name": "web_fetch",
            "input": {"url": "https://example.com/path", "method": "post", "timeout": "12.5"},
        },
        GuardDecision(
            stage="permission",
            allowed=False,
            category="network",
            reason_code="permission_required",
            mode="standard",
            policy_decision="ask",
            required_allow="network",
        ),
    )

    assert decision.url_preview == "https://example.com/path"
    assert decision.host == "example.com"
    assert decision.method == "POST"
    assert decision.timeout_seconds == 12.5
    assert decision.process_label == ""


@pytest.mark.asyncio
async def test_tool_confirm_deny_keeps_permission_denied_result():
    from personal_agent.conversation.events import EventRecorder
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.executor import execute_tool_call_result
    from personal_agent.tools.registry import tool_registry

    original = tool_registry.get("confirm_write_demo")
    calls = 0
    decisions = []

    async def handler():
        nonlocal calls
        calls += 1
        return "ok"

    async def confirm(decision):
        decisions.append(decision)
        return "deny"

    tool_registry.register(ToolEntry(
        name="confirm_write_demo",
        description="confirm",
        schema={},
        handler=handler,
        permission_category="write",
        is_destructive=True,
    ))
    agent = MockAgent()
    agent._execution_policy = ExecutionPolicy(
        mode="standard",
        permissions={"default": "allow", "write": "ask"},
    )
    recorder = EventRecorder()

    try:
        result = await execute_tool_call_result(
            {"id": "c1", "name": "confirm_write_demo", "input": {}},
            agent=agent,
            event_sink=recorder,
            confirm=confirm,
        )
    finally:
        if original is None:
            tool_registry.unregister("confirm_write_demo")
        else:
            tool_registry.register(original)

    assert result.status == "denied"
    assert result.category == "authorization"
    assert calls == 0
    assert len(decisions) == 1
    assert decisions[0].tool_name == "confirm_write_demo"
    assert decisions[0].permission_category == "write"
    assert recorder.events[-2].data["reason_code"] == "permission_required"
    assert recorder.events[-1].data["status"] == "denied"
    assert "write" not in agent._destructive_allowed


@pytest.mark.asyncio
async def test_tool_confirm_allow_executes_once_without_persisting_grant():
    from personal_agent.conversation.events import EventRecorder
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.executor import execute_tool_call_result
    from personal_agent.tools.registry import tool_registry

    original = tool_registry.get("confirm_allow_demo")
    calls = 0
    decisions = []

    async def handler():
        nonlocal calls
        calls += 1
        return "ok"

    async def confirm(decision):
        decisions.append(decision)
        return "allow"

    tool_registry.register(ToolEntry(
        name="confirm_allow_demo",
        description="confirm",
        schema={},
        handler=handler,
        permission_category="write",
        is_destructive=True,
    ))
    agent = MockAgent()
    agent._execution_policy = ExecutionPolicy(
        mode="standard",
        permissions={"default": "allow", "write": "ask"},
    )
    recorder = EventRecorder()

    try:
        result = await execute_tool_call_result(
            {"id": "c1", "name": "confirm_allow_demo", "input": {}},
            agent=agent,
            event_sink=recorder,
            confirm=confirm,
        )
    finally:
        if original is None:
            tool_registry.unregister("confirm_allow_demo")
        else:
            tool_registry.register(original)

    assert result.status == "success"
    assert result.content == "ok"
    assert calls == 1
    assert len(decisions) == 1
    assert recorder.events[-2].data["allowed"] is True
    assert recorder.events[-2].data["stage"] == "runtime_guard"
    assert recorder.events[-1].data["status"] == "success"
    assert "write" not in agent._destructive_allowed


@pytest.mark.asyncio
async def test_tool_confirm_always_persists_grant_for_later_tool_calls():
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.executor import execute_tool_calls
    from personal_agent.tools.registry import tool_registry

    original = tool_registry.get("confirm_always_demo")
    calls = 0
    decisions = []

    async def handler():
        nonlocal calls
        calls += 1
        return f"ok:{calls}"

    async def confirm(decision):
        decisions.append(decision)
        return "always"

    tool_registry.register(ToolEntry(
        name="confirm_always_demo",
        description="confirm",
        schema={},
        handler=handler,
        permission_category="write",
        is_destructive=True,
    ))
    agent = MockAgent()
    agent._execution_policy = ExecutionPolicy(
        mode="standard",
        permissions={"default": "allow", "write": "ask"},
    )
    messages = []

    try:
        results = await execute_tool_calls(
            [
                {"id": "c1", "name": "confirm_always_demo", "input": {}},
                {"id": "c2", "name": "confirm_always_demo", "input": {}},
            ],
            messages,
            agent=agent,
            confirm=confirm,
        )
    finally:
        if original is None:
            tool_registry.unregister("confirm_always_demo")
        else:
            tool_registry.register(original)

    assert [result.status for result in results] == ["success", "success"]
    assert [result.content for result in results] == ["ok:1", "ok:2"]
    assert calls == 2
    assert len(decisions) == 1
    assert "write" in agent._destructive_allowed
    assert "write" in agent._temporary_grants
    assert messages[-1]["content"][0]["content"] == "ok:1"
    assert messages[-1]["content"][1]["content"] == "ok:2"


@pytest.mark.asyncio
async def test_parallel_safe_tools_requiring_confirm_are_serialized():
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.executor import execute_tool_calls
    from personal_agent.tools.registry import tool_registry

    original_first = tool_registry.get("confirm_parallel_first")
    original_second = tool_registry.get("confirm_parallel_second")
    running_confirms = 0
    max_running_confirms = 0
    decisions = []

    async def first():
        return "first"

    async def second():
        return "second"

    async def confirm(decision):
        nonlocal running_confirms, max_running_confirms
        decisions.append(decision.tool_name)
        running_confirms += 1
        max_running_confirms = max(max_running_confirms, running_confirms)
        await asyncio.sleep(0)
        running_confirms -= 1
        return "allow"

    tool_registry.register(ToolEntry(
        name="confirm_parallel_first",
        description="confirm",
        schema={},
        handler=first,
        permission_category="network",
        is_parallel_safe=True,
    ))
    tool_registry.register(ToolEntry(
        name="confirm_parallel_second",
        description="confirm",
        schema={},
        handler=second,
        permission_category="network",
        is_parallel_safe=True,
    ))
    agent = MockAgent()
    agent._execution_policy = ExecutionPolicy(
        mode="trusted",
        permissions={"default": "allow", "network": "ask"},
    )
    messages = []

    try:
        results = await execute_tool_calls(
            [
                {"id": "c1", "name": "confirm_parallel_first", "input": {}},
                {"id": "c2", "name": "confirm_parallel_second", "input": {}},
            ],
            messages,
            agent=agent,
            confirm=confirm,
        )
    finally:
        for name, original in (
            ("confirm_parallel_first", original_first),
            ("confirm_parallel_second", original_second),
        ):
            if original is None:
                tool_registry.unregister(name)
            else:
                tool_registry.register(original)

    assert [result.status for result in results] == ["success", "success"]
    assert [result.content for result in results] == ["first", "second"]
    assert decisions == ["confirm_parallel_first", "confirm_parallel_second"]
    assert max_running_confirms == 1
    assert messages[-1]["content"][0]["content"] == "first"
    assert messages[-1]["content"][1]["content"] == "second"


@pytest.mark.asyncio
async def test_tool_confirm_not_called_for_hard_precheck_denial(tmp_path: Path):
    import personal_agent.plugins.builtin.tools.builtin.process_tool  # noqa: F401
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.plugins.builtin.tools.builtin.bash import set_work_dir
    from personal_agent.tools.executor import execute_tool_call_result
    from personal_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])
    set_work_dir(tmp_path)
    agent = MockAgent()
    agent._execution_policy = ExecutionPolicy(
        mode="standard",
        permissions={"default": "allow", "background": "ask"},
    )
    confirm_calls = 0

    async def confirm(_decision):
        nonlocal confirm_calls
        confirm_calls += 1
        return "always"

    result = await execute_tool_call_result(
        {"id": "p0", "name": "process_start", "input": {"command": "rm -rf /"}},
        agent=agent,
        confirm=confirm,
    )

    assert result.status == "denied"
    assert result.category == "precheck"
    assert confirm_calls == 0


@pytest.mark.asyncio
async def test_tool_confirm_pending_stop_denies_without_executing():
    from personal_agent.conversation.events import EventRecorder
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.executor import clear_interrupted, execute_tool_call_result
    from personal_agent.tools.registry import tool_registry

    original = tool_registry.get("confirm_interrupt_demo")
    calls = 0
    confirm_started = asyncio.Event()
    confirm_cancelled = asyncio.Event()

    async def handler():
        nonlocal calls
        calls += 1
        return "ok"

    async def confirm(_decision):
        confirm_started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            confirm_cancelled.set()
            raise

    tool_registry.register(ToolEntry(
        name="confirm_interrupt_demo",
        description="confirm",
        schema={},
        handler=handler,
        permission_category="write",
        is_destructive=True,
    ))
    agent = MockAgent()
    agent._execution_policy = ExecutionPolicy(
        mode="standard",
        permissions={"default": "allow", "write": "ask"},
    )
    recorder = EventRecorder()

    try:
        task = asyncio.create_task(execute_tool_call_result(
            {"id": "c-stop", "name": "confirm_interrupt_demo", "input": {}},
            agent=agent,
            event_sink=recorder,
            confirm=confirm,
        ))
        await asyncio.wait_for(confirm_started.wait(), timeout=1)
        agent._interrupt_requested = True
        result = await asyncio.wait_for(task, timeout=1)
    finally:
        clear_interrupted()
        if original is None:
            tool_registry.unregister("confirm_interrupt_demo")
        else:
            tool_registry.register(original)

    assert result.status == "denied"
    assert result.category == "authorization"
    assert result.error == "tool confirmation interrupted"
    assert calls == 0
    assert confirm_cancelled.is_set()
    assert "write" not in agent._destructive_allowed
    assert [event.type for event in recorder.events] == [
        "tool_start",
        "tool_decision",
        "tool_end",
    ]
    assert recorder.events[-1].data["status"] == "denied"
    assert recorder.events[-1].data["guard_reason_code"] == "permission_required"


@pytest.mark.asyncio
async def test_tool_end_event_includes_guard_metadata_for_success():
    from personal_agent.conversation.events import EventRecorder
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.executor import execute_tool_call_result
    from personal_agent.tools.registry import tool_registry

    original = tool_registry.get("metadata_demo")
    recorder = EventRecorder()

    async def handler():
        return "ok"

    tool_registry.register(ToolEntry(
        name="metadata_demo",
        description="metadata",
        schema={},
        handler=handler,
        permission_category="read",
    ))

    try:
        result = await execute_tool_call_result(
            {"id": "m1", "name": "metadata_demo", "input": {}},
            event_sink=recorder,
        )
    finally:
        if original is None:
            tool_registry.unregister("metadata_demo")
        else:
            tool_registry.register(original)

    decision_event = recorder.events[-2]
    event = recorder.events[-1]
    assert result.status == "success"
    assert [item.type for item in recorder.events] == ["tool_start", "tool_decision", "tool_end"]
    assert decision_event.type == "tool_decision"
    assert decision_event.data["allowed"] is True
    assert decision_event.data["stage"] == "runtime_guard"
    assert decision_event.data["status"] == "allowed"
    assert event.data["guard_stage"] == "runtime_guard"
    assert event.data["permission_category"] == "read"


@pytest.mark.asyncio
async def test_structured_tool_output_keeps_artifacts_in_memory_and_redacts_events():
    from personal_agent.conversation.events import EventRecorder
    from personal_agent.tools.entry import ToolArtifact, ToolEntry, ToolHandlerOutput
    from personal_agent.tools.executor import execute_tool_call_result
    from personal_agent.tools.registry import tool_registry

    recorder = EventRecorder()

    async def handler():
        return ToolHandlerOutput(
            text="[image: image/png]",
            artifacts=[ToolArtifact(kind="image", mime_type="image/png", data="base64-secret")],
            metadata={
                "mcp_server": "images",
                "remote_tool": "render",
                "structured_content": {"private": "value"},
            },
        )

    tool_registry.register(ToolEntry(
        name="structured_artifact_demo",
        description="structured artifact",
        schema={},
        handler=handler,
        permission_category="read",
    ))
    try:
        result = await execute_tool_call_result(
            {"id": "artifact-1", "name": "structured_artifact_demo", "input": {}},
            event_sink=recorder,
        )
    finally:
        tool_registry.unregister("structured_artifact_demo")

    event = recorder.events[-1]
    serialized = result.as_dict()
    assert result.artifacts[0].data == "base64-secret"
    assert serialized["artifacts"][0]["has_data"] is True
    assert "data" not in serialized["artifacts"][0]
    assert serialized["result_metadata"]["structured_content_present"] is True
    assert event.data["artifact_count"] == 1
    assert "base64-secret" not in str(event.as_dict())
    assert "private" not in str(event.as_dict())


# ── Executor _exec_one integration ─────────────────────


@pytest.mark.asyncio
async def test_exec_one_blocks_destructive_without_allow():
    from personal_agent.tools.executor import _exec_one

    agent = MockAgent()
    tc = {"name": "write", "input": {"path": "test.txt", "content": "hello"}}

    result = await _exec_one(tc, agent=agent)
    assert "authorization" in result.lower() or "allow" in result.lower()


@pytest.mark.asyncio
async def test_exec_one_unknown_tool():
    from personal_agent.tools.executor import _exec_one

    tc = {"name": "nonexistent_tool_xyz", "input": {}}
    result = await _exec_one(tc)
    assert "unknown" in result.lower()


@pytest.mark.asyncio
async def test_execute_tool_call_result_structured_success_and_error():
    from personal_agent.conversation.events import EventRecorder
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.executor import execute_tool_call_result, format_tool_result
    from personal_agent.tools.registry import tool_registry

    original = tool_registry.get("structured_demo")
    recorder = EventRecorder()

    async def handler(value):
        return {"value": value}

    tool_registry.register(ToolEntry(
        name="structured_demo",
        description="structured",
        schema={},
        handler=handler,
    ))

    try:
        result = await execute_tool_call_result({
            "id": "call-1",
            "name": "structured_demo",
            "input": {"value": 7},
        }, event_sink=recorder)
        unknown = await execute_tool_call_result({"id": "bad", "name": "missing_demo", "input": {}})
    finally:
        if original is None:
            tool_registry.unregister("structured_demo")
        else:
            tool_registry.register(original)

    assert result.status == "success"
    assert result.tool_name == "structured_demo"
    assert result.tool_use_id == "call-1"
    assert result.content == '{"value": 7}'
    assert result.input_summary == '{"value": 7}'
    assert format_tool_result(result) == '{"value": 7}'
    assert [event.type for event in recorder.events] == ["tool_start", "tool_decision", "tool_end"]
    assert recorder.events[0].data["tool_name"] == "structured_demo"
    assert recorder.events[1].data["allowed"] is True
    assert recorder.events[2].data["status"] == "success"
    assert recorder.events[2].data["output_truncated"] is False
    assert unknown.status == "error"
    assert unknown.category == "unknown_tool"
    assert "unknown tool" in format_tool_result(unknown)


@pytest.mark.asyncio
async def test_tool_end_marks_truncated_output():
    from personal_agent.conversation.events import EventRecorder
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.executor import MAX_RESULT_CHARS, execute_tool_call_result
    from personal_agent.tools.registry import tool_registry

    original = tool_registry.get("large_output_demo")
    recorder = EventRecorder()

    async def handler():
        return "x" * (MAX_RESULT_CHARS + 20)

    tool_registry.register(ToolEntry(
        name="large_output_demo",
        description="large",
        schema={},
        handler=handler,
    ))

    try:
        result = await execute_tool_call_result({
            "id": "call-large",
            "name": "large_output_demo",
            "input": {},
        }, event_sink=recorder)
    finally:
        if original is None:
            tool_registry.unregister("large_output_demo")
        else:
            tool_registry.register(original)

    tool_end = recorder.events[-1]
    assert result.output_truncated is True
    assert tool_end.type == "tool_end"
    assert tool_end.data["output_truncated"] is True
    assert len(tool_end.data["full_output"]) > MAX_RESULT_CHARS


@pytest.mark.asyncio
async def test_unknown_tool_emits_tool_decision():
    from personal_agent.conversation.events import EventRecorder
    from personal_agent.tools.executor import execute_tool_call_result

    recorder = EventRecorder()

    result = await execute_tool_call_result(
        {"id": "bad", "name": "missing_demo", "input": {}},
        event_sink=recorder,
    )

    assert result.status == "error"
    assert [event.type for event in recorder.events] == ["tool_start", "tool_decision", "tool_end"]
    assert recorder.events[1].data["stage"] == "lookup"
    assert recorder.events[1].data["status"] == "error"
    assert recorder.events[1].data["reason_code"] == "unknown_tool"


@pytest.mark.asyncio
async def test_execute_tool_call_result_timeout_and_interrupt():
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.executor import (
        clear_interrupted,
        execute_tool_call_result,
        set_interrupted,
    )
    from personal_agent.tools.registry import tool_registry

    original = tool_registry.get("slow_demo")

    async def slow():
        await asyncio.sleep(1)
        return "late"

    tool_registry.register(ToolEntry(
        name="slow_demo",
        description="slow",
        schema={},
        handler=slow,
    ))

    try:
        timed_out = await execute_tool_call_result(
            {"id": "slow-1", "name": "slow_demo", "input": {}},
            timeout=0.01,
        )
        set_interrupted()
        interrupted = await execute_tool_call_result({"id": "slow-2", "name": "slow_demo", "input": {}})
    finally:
        clear_interrupted()
        if original is None:
            tool_registry.unregister("slow_demo")
        else:
            tool_registry.register(original)

    assert timed_out.status == "timeout"
    assert timed_out.category == "timeout"
    assert "timed out" in timed_out.error
    assert interrupted.status == "interrupted"
    assert interrupted.category == "interrupt"


@pytest.mark.asyncio
async def test_execute_tool_calls_preserves_order_and_records_agent_summary():
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.executor import execute_tool_calls
    from personal_agent.tools.registry import tool_registry

    agent = MockAgent()
    original_first = tool_registry.get("parallel_first")
    original_second = tool_registry.get("parallel_second")

    async def first():
        await asyncio.sleep(0.02)
        return "first"

    async def second():
        return "second"

    tool_registry.register(ToolEntry(
        name="parallel_first",
        description="first",
        schema={},
        handler=first,
        is_parallel_safe=True,
    ))
    tool_registry.register(ToolEntry(
        name="parallel_second",
        description="second",
        schema={},
        handler=second,
        is_parallel_safe=True,
    ))

    messages = []
    try:
        results = await execute_tool_calls(
            [
                {"id": "a", "name": "parallel_first", "input": {}},
                {"id": "b", "name": "parallel_second", "input": {}},
            ],
            messages,
            agent=agent,
        )
    finally:
        if original_first is None:
            tool_registry.unregister("parallel_first")
        else:
            tool_registry.register(original_first)
        if original_second is None:
            tool_registry.unregister("parallel_second")
        else:
            tool_registry.register(original_second)

    assert [item.status for item in results] == ["success", "success"]
    assert messages[-1]["content"] == [
        {"type": "tool_result", "tool_use_id": "a", "content": "first"},
        {"type": "tool_result", "tool_use_id": "b", "content": "second"},
    ]
    assert [item["tool_name"] for item in agent._last_tool_results] == [
        "parallel_first",
        "parallel_second",
    ]


@pytest.mark.asyncio
async def test_bridge_tool_call_uses_executor_precheck_for_web_fetch():
    import personal_agent.plugins.builtin.tools.builtin.web_fetch  # noqa: F401
    from personal_agent.plugins.builtin.tools.bridge.bridge import _tool_call

    result = await _tool_call("web_fetch", {"url": "http://127.0.0.1/admin"})

    assert "private" in result.lower() or "blocked" in result.lower() or "unsafe" in result.lower()


# ── Audit module ───────────────────────────────────────


def test_audit_imports():
    """Verify audit module exists and has expected API."""
    from personal_agent.tools.audit import audit_log, audit_tool_decision, audit_tool_result, set_audit_path
    assert callable(audit_log)
    assert callable(audit_tool_decision)
    assert callable(audit_tool_result)
    assert callable(set_audit_path)


@pytest.mark.asyncio
async def test_audit_writes_log():
    """Verify audit_log actually writes to the configured path."""
    from personal_agent.tools.audit import audit_log, set_audit_path

    with tempfile.TemporaryDirectory() as tmpdir:
        audit_path = Path(tmpdir) / "audit.log"
        set_audit_path(audit_path)

        audit_log("test_tool", "test_target", "test result", True)
        audit_log("test_tool", "test_target", "error message", False)

        # Give async writer a moment
        await asyncio.sleep(0.2)

        assert audit_path.exists()
        lines = audit_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert "test_tool" in lines[0]
        assert "test result" in lines[0]
        assert "test_tool" in lines[1]
        assert "error message" in lines[1]


@pytest.mark.asyncio
async def test_audit_tool_decision_writes_structured_record():
    from personal_agent.tools.audit import audit_tool_decision, set_audit_path
    from personal_agent.tools.execution_guard import ToolDecision

    with tempfile.TemporaryDirectory() as tmpdir:
        audit_path = Path(tmpdir) / "audit.log"
        set_audit_path(audit_path)

        audit_tool_decision(ToolDecision(
            tool_name="write",
            tool_use_id="call-1",
            allowed=False,
            stage="permission",
            status="denied",
            permission_category="write",
            execution_mode="standard",
            permission_decision="ask",
            reason_code="permission_required",
            required_allow="write",
            message="requires authorization",
        ))

        await asyncio.sleep(0.2)

        line = audit_path.read_text(encoding="utf-8").strip()
        data = json.loads(line)
        assert data["event"] == "tool_decision"
        assert data["tool"] == "write"
        assert data["tool_use_id"] == "call-1"
        assert data["allowed"] is False
        assert data["stage"] == "permission"
        assert data["status"] == "denied"
        assert data["permission_category"] == "write"
        assert data["execution_mode"] == "standard"
        assert data["permission_decision"] == "ask"
        assert data["reason_code"] == "permission_required"
        assert data["required_allow"] == "write"


@pytest.mark.asyncio
async def test_audit_tool_result_writes_structured_record():
    from personal_agent.tools.audit import audit_tool_result, set_audit_path
    from personal_agent.tools.execution_guard import ToolDecision
    from personal_agent.tools.executor import ToolExecutionResult

    with tempfile.TemporaryDirectory() as tmpdir:
        audit_path = Path(tmpdir) / "audit.log"
        set_audit_path(audit_path)

        audit_tool_result(
            ToolExecutionResult(
                tool_name="write",
                tool_use_id="call-1",
                status="denied",
                category="authorization",
                error="requires authorization",
                duration=0.25,
                attempts=1,
                input_summary='{"api_key": "sk-proj-abcdefghijklmnopqrstuvwxyz"}',
                output_summary="requires authorization",
            ),
            decision=ToolDecision(
                tool_name="write",
                tool_use_id="call-1",
                allowed=False,
                stage="permission",
                status="denied",
                permission_category="write",
                execution_mode="standard",
                permission_decision="ask",
                reason_code="permission_required",
                required_allow="write",
                grant_matched="",
            ),
        )

        line = audit_path.read_text(encoding="utf-8").strip()
        data = json.loads(line)
        assert data["event"] == "tool_result"
        assert data["tool"] == "write"
        assert data["tool_use_id"] == "call-1"
        assert data["status"] == "denied"
        assert data["category"] == "authorization"
        assert data["permission_category"] == "write"
        assert data["execution_mode"] == "standard"
        assert data["permission_decision"] == "ask"
        assert data["reason_code"] == "permission_required"
        assert data["required_allow"] == "write"
        assert data["duration"] == 0.25
        assert data["attempts"] == 1
        assert "abcdefghijklmnopqrstuvwxyz" not in data["input_summary"]
        assert "authorization" in data["output_summary"]


@pytest.mark.asyncio
async def test_executor_writes_decision_and_result_audit_without_handler_duplicate(tmp_path: Path):
    import personal_agent.plugins.builtin.tools.builtin.file_write  # noqa: F401
    from personal_agent.tools.audit import set_audit_path
    from personal_agent.tools.executor import execute_tool_call_result
    from personal_agent.tools.sandbox import init_sandbox
    from personal_agent.security.session import SecurityStateStore
    from types import SimpleNamespace

    audit_path = tmp_path / "audit.log"
    set_audit_path(audit_path)
    init_sandbox([tmp_path], [])

    settings = SimpleNamespace(
        execution_mode="full-auto",
        sandbox_roots=[tmp_path],
        permission_grant_ttl_minutes=60,
    )
    agent = SimpleNamespace(
        _security_context=SecurityStateStore(settings).context("audit"),
        _tool_calls_this_turn=0,
        _max_tool_calls_per_turn=20,
        _destructive_calls_this_turn=0,
        _max_destructive_per_turn=3,
        _interrupt_requested=False,
    )
    result = await execute_tool_call_result({
        "id": "write-1",
        "name": "write",
        "input": {"path": "result-audit.txt", "content": "ok"},
    }, agent=agent)

    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]
    assert result.status == "success"
    assert [event.get("event") for event in events] == ["tool_decision", "tool_result"]
    assert events[0]["tool"] == "write"
    assert events[0]["allowed"] is True
    assert events[1]["tool"] == "write"
    assert events[1]["status"] == "success"
    assert events[1]["permission_category"] == "write"
    assert "success" not in events[1]


@pytest.mark.asyncio
async def test_executor_writes_result_audit_for_denied_precheck_and_unknown_tools(tmp_path: Path):
    import personal_agent.plugins.builtin.tools.builtin.process_tool  # noqa: F401
    from personal_agent.execution import ExecutionPolicy
    from personal_agent.tools.audit import set_audit_path
    from personal_agent.tools.executor import execute_tool_call_result

    audit_path = tmp_path / "audit.log"
    set_audit_path(audit_path)

    agent = MockAgent()
    agent._execution_policy = ExecutionPolicy(
        mode="standard",
        permissions={"default": "allow", "background": "ask"},
    )

    precheck = await execute_tool_call_result(
        {"id": "p0", "name": "process_start", "input": {"command": "rm -rf /"}},
        agent=agent,
    )
    denied = await execute_tool_call_result(
        {"id": "p1", "name": "process_start", "input": {"command": "python -c \"print(1)\""}},
        agent=agent,
    )
    unknown = await execute_tool_call_result({"id": "bad", "name": "missing_demo", "input": {}})

    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").strip().splitlines()]
    assert precheck.status == "denied"
    assert denied.status == "denied"
    assert denied.guard_stage == "permission"
    assert denied.reason_code == "permission_required"
    assert denied.permission_category == "background"
    assert denied.permission_decision == "ask"
    assert denied.required_allow == "background"
    assert denied.execution_mode == "standard"
    assert unknown.status == "error"
    assert [event["event"] for event in events] == [
        "tool_decision",
        "tool_result",
        "tool_decision",
        "tool_result",
        "tool_decision",
        "tool_result",
    ]
    assert events[1]["tool"] == "process_start"
    assert events[1]["status"] == "denied"
    assert events[1]["category"] == "precheck"
    assert events[1]["reason_code"] == "hard_blacklist"
    assert events[3]["tool"] == "process_start"
    assert events[3]["status"] == "denied"
    assert events[3]["reason_code"] == "permission_required"
    assert events[3]["required_allow"] == "background"
    assert events[5]["tool"] == "missing_demo"
    assert events[5]["status"] == "error"
    assert events[5]["category"] == "unknown_tool"
    assert events[5]["reason_code"] == "unknown_tool"


# ── Checkpoint (file_write backup) ─────────────────────


def test_checkpoint_creates_backup(tmp_path: Path):
    from personal_agent.tools.sandbox import init_sandbox
    from personal_agent.tools.executor import _checkpoint_file_write

    # Redirect sandbox to tmp_path
    init_sandbox([tmp_path], [])

    # Create a file to be modified
    target = tmp_path / "test.txt"
    target.write_text("original content")

    tc = {"name": "write", "input": {"path": "test.txt", "content": "new"}}
    _checkpoint_file_write(tc)

    # Verify backup exists
    checkpoints = tmp_path / "checkpoints"
    assert checkpoints.exists()
    backups = list(checkpoints.glob("test.txt.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text() == "original content"


def test_checkpoint_noop_for_new_file(tmp_path: Path):
    from personal_agent.tools.sandbox import init_sandbox
    from personal_agent.tools.executor import _checkpoint_file_write

    init_sandbox([tmp_path], [])
    tc = {"name": "write", "input": {"path": "new_file.txt", "content": "new"}}
    _checkpoint_file_write(tc)

    # No backup should be created for new file
    checkpoints = tmp_path / "checkpoints"
    assert not checkpoints.exists() or len(list(checkpoints.glob("*.bak"))) == 0


# ── BM25 search now returns input_schema ───────────────


def test_bm25_search_returns_schema():
    from personal_agent.tools.registry import _bm25_search

    catalog = [
        {"name": "weather", "description": "Get weather forecast", "input_schema": {
            "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]
        }},
        {"name": "calculator", "description": "Calculate math expression", "input_schema": {
            "type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]
        }},
    ]

    results = _bm25_search(catalog, "weather")
    assert len(results) > 0
    assert "input_schema" in results[0]
    assert results[0]["input_schema"] is not None
    assert "city" in str(results[0]["input_schema"])


# ── Bash hard blacklist ──────────────────────────────────


def test_bash_hard_blacklist_catastrophic():
    """Hard blacklist blocks catastrophic commands unconditionally."""
    from personal_agent.plugins.builtin.tools.builtin.bash import _check_command

    # These must ALWAYS be blocked (even with /allow bash)
    catastrophic = [
        "rm -rf /",
        "rm -rf /*",
        "rm -rf ~",
        "rm -rf /etc",
        "rm -rf $HOME",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sda1",
        "mkfs /dev/sda1",
        "shutdown -h now",
        "reboot",
        "poweroff",
        "chmod 777 /",
        "> /etc/passwd",
        ":(){ :|:& };:",
    ]
    for cmd in catastrophic:
        result = _check_command(cmd)
        assert result is not None, f"'{cmd}' must be blocked by hard blacklist"
        assert "hard blacklist" in result.lower()


def test_bash_hard_blacklist_sudo_wrappers():
    """Hard blacklist catches sudo-wrapped catastrophic commands."""
    from personal_agent.plugins.builtin.tools.builtin.bash import _check_command

    assert _check_command("sudo rm -rf /") is not None
    assert _check_command("sudo shutdown -h now") is not None
    assert _check_command("sudo reboot") is not None


def test_bash_normal_rm_not_blocked():
    """Normal rm operations (not targeting root) should pass hard blacklist."""
    from personal_agent.plugins.builtin.tools.builtin.bash import _check_command

    # Single file removal — NOT caught by hard blacklist
    result = _check_command("rm file.txt")
    assert result is None  # passes hard blacklist, reaches whitelist


# ── File read sensitive blocking ─────────────────────────


def test_sandbox_blocks_env(tmp_path: Path):
    """Blocked patterns prevent access to .env files."""
    from personal_agent.tools.sandbox import init_sandbox, get_sandbox

    init_sandbox([tmp_path], ["**/.env", "**/.env.*"])
    sandbox = get_sandbox()
    assert sandbox.check_path(tmp_path / ".env") is not None
    assert sandbox.check_path(tmp_path / "project" / ".env.local") is not None


def test_sandbox_blocks_ssh_keys(tmp_path: Path):
    """Blocked patterns prevent access to SSH keys."""
    from personal_agent.tools.sandbox import init_sandbox, get_sandbox

    init_sandbox([tmp_path], ["**/.ssh/**", "**/id_rsa*", "**/id_ed*"])
    sandbox = get_sandbox()
    assert sandbox.check_path(tmp_path / "id_rsa") is not None
    assert sandbox.check_path(tmp_path / "id_ed25519") is not None
    assert sandbox.check_path(tmp_path / ".ssh" / "id_rsa") is not None


def test_sandbox_blocks_credential_files(tmp_path: Path):
    """Blocked patterns prevent access to credential files."""
    from personal_agent.tools.sandbox import init_sandbox, get_sandbox

    init_sandbox([tmp_path], ["**/.netrc", "**/.pgpass", "**/.npmrc"])
    sandbox = get_sandbox()
    assert sandbox.check_path(tmp_path / ".netrc") is not None
    assert sandbox.check_path(tmp_path / ".pgpass") is not None
    assert sandbox.check_path(tmp_path / "project" / ".npmrc") is not None


def test_sandbox_allows_normal_files(tmp_path: Path):
    """Normal files pass sandbox check."""
    from personal_agent.tools.sandbox import init_sandbox, get_sandbox

    init_sandbox([tmp_path], ["**/.env", "**/.git/**", "**/.ssh/**"])
    sandbox = get_sandbox()
    assert sandbox.check_path(tmp_path / "notes.txt") is None
    assert sandbox.check_path(tmp_path / "src" / "main.py") is None
    assert sandbox.check_path(tmp_path / "README.md") is None


# ── SSRF URL safety ──────────────────────────────────────


def test_url_safety_blocks_private_ips():
    from personal_agent.tools.url_safety import check_url

    assert check_url("http://127.0.0.1/admin") is not None
    assert check_url("http://localhost/api") is not None
    assert check_url("http://10.0.0.1/") is not None
    assert check_url("http://192.168.1.1/") is not None
    assert check_url("http://172.16.0.1/") is not None


def test_url_safety_blocks_metadata_endpoints():
    from personal_agent.tools.url_safety import check_url

    assert check_url("http://169.254.169.254/latest/meta-data") is not None
    assert check_url("http://metadata.google.internal/") is not None


def test_url_safety_allows_public_ips():
    from personal_agent.tools.url_safety import check_url

    # Use IPs that are definitely public (not DNS-dependent)
    assert check_url("https://1.1.1.1") is None
    assert check_url("https://8.8.8.8") is None
    assert check_url("https://93.184.216.34") is None  # example.com IP


def test_url_safety_blocks_multicast_linklocal():
    from personal_agent.tools.url_safety import check_url

    assert check_url("http://224.0.0.1/") is not None  # multicast
    assert check_url("http://169.254.1.1/") is not None  # link-local
    assert check_url("http://0.0.0.0/") is not None  # unspecified


# ── Credential env filtering ────────────────────────────


def test_env_filter_blocks_api_keys():
    from personal_agent.tools.env_filter import filter_env

    env = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "LLM_API_KEY": "sk-secret-key-12345",
        "OPENAI_API_KEY": "sk-openai-67890",
        "DEEPSEEK_API_KEY": "sk-deepseek-abcde",
        "FEISHU_APP_SECRET": "secret123",
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "WEIXIN_TOKEN": "wx_token",
        "QQ_BOT_TOKEN": "qq_token",
        "QQ_BOT_WEBHOOK_SECRET": "qq_secret",
        "GITHUB_TOKEN": "ghp_secret123",
        "NORMAL_VAR": "hello",
    }
    filtered = filter_env(env)
    assert "PATH" in filtered
    assert "HOME" in filtered
    assert "NORMAL_VAR" in filtered
    assert "LLM_API_KEY" not in filtered
    assert "OPENAI_API_KEY" not in filtered
    assert "DEEPSEEK_API_KEY" not in filtered
    assert "FEISHU_APP_SECRET" not in filtered
    assert "TELEGRAM_BOT_TOKEN" not in filtered
    assert "WEIXIN_TOKEN" not in filtered
    assert "QQ_BOT_TOKEN" not in filtered
    assert "QQ_BOT_WEBHOOK_SECRET" not in filtered
    assert "GITHUB_TOKEN" not in filtered


def test_env_filter_blocks_anthropic_prefixes():
    from personal_agent.tools.env_filter import filter_env

    env = {"ANTHROPIC_API_KEY": "sk-ant-secret", "ANTHROPIC_BASE_URL": "https://..."}
    filtered = filter_env(env)
    assert "ANTHROPIC_API_KEY" not in filtered
    assert "ANTHROPIC_BASE_URL" not in filtered


# ── Log redaction ────────────────────────────────────────


def test_redact_masks_openai_key():
    from personal_agent.tools.redact import redact

    text = "Using API key: sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    result = redact(text)
    assert "sk-proj-abcdefghi" not in result  # full key gone
    assert "****" in result


def test_redact_masks_github_token():
    from personal_agent.tools.redact import redact

    text = "Token: ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    result = redact(text)
    assert "ghp_abcdefghijklmnop" not in result
    assert "****" in result


def test_redact_masks_jwt():
    from personal_agent.tools.redact import redact

    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    result = redact("Bearer " + jwt)
    assert jwt not in result
    assert "****" in result


def test_redact_masks_auth_header():
    from personal_agent.tools.redact import redact

    text = "Authorization: Bearer sk-ant-api03-secret-key-here-12345"
    result = redact(text)
    assert "sk-ant-api03-secret" not in result
    assert "****" in result or "*" in result


def test_redact_preserves_normal_text():
    from personal_agent.tools.redact import redact

    text = "Hello world! The weather is nice today."
    assert redact(text) == text


def test_redact_empty_string():
    from personal_agent.tools.redact import redact

    assert redact("") == ""
    assert redact(None) is None
