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
    )

    blocked = _scope_gate(tc, entry, agent)
    assert blocked is not None
    assert "background" in blocked
    agent._destructive_allowed.add("background")
    assert _scope_gate(tc, entry, agent) is None


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
    )

    result = _scope_gate(tc, entry, agent)
    assert result is not None
    assert "denied by execution mode" in result


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
    assert [event.type for event in recorder.events] == ["tool_start", "tool_end"]
    assert recorder.events[0].data["tool_name"] == "structured_demo"
    assert recorder.events[1].data["status"] == "success"
    assert unknown.status == "error"
    assert unknown.category == "unknown_tool"
    assert "unknown tool" in format_tool_result(unknown)


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
    from personal_agent.tools.audit import audit_log, set_audit_path
    assert callable(audit_log)
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
