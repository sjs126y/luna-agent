from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def _agent(tmp_path: Path, *, mode: str = "ask-first"):
    from luna_agent.security.session import SecurityStateStore

    settings = SimpleNamespace(
        execution_mode=mode,
        sandbox_roots=[tmp_path],
        permission_grant_ttl_minutes=60,
    )
    store = SecurityStateStore(settings)
    return SimpleNamespace(
        _security_context=store.context("session-a"),
        _security_grant_ttl_seconds=store.grant_ttl_seconds,
        _tool_calls_this_turn=0,
        _max_tool_calls_per_turn=20,
        _destructive_calls_this_turn=0,
        _max_destructive_per_turn=3,
        _interrupt_requested=False,
    )


@pytest.mark.asyncio
async def test_cached_tool_approval_uses_session_ttl(tmp_path):
    from luna_agent.tools.entry import ToolEntry
    from luna_agent.tools.executor import execute_tool_call_result
    from luna_agent.tools.registry import tool_registry

    calls: list[str] = []
    confirmations = 0

    async def handler():
        calls.append("run")
        return "ok"

    async def confirm(_decision):
        nonlocal confirmations
        confirmations += 1
        return "always"

    entry = ToolEntry("external_cached_demo", "demo", {}, handler, approval_mode="cached")
    tool_registry.register(entry)
    agent = _agent(tmp_path)
    try:
        first = await execute_tool_call_result(
            {"id": "one", "name": entry.name, "input": {}}, agent=agent, confirm=confirm
        )
        second = await execute_tool_call_result(
            {"id": "two", "name": entry.name, "input": {}}, agent=agent, confirm=confirm
        )
    finally:
        tool_registry.unregister(entry.name)

    assert first.status == second.status == "success"
    assert calls == ["run", "run"]
    assert confirmations == 1


@pytest.mark.asyncio
async def test_local_auto_runs_cached_tool_without_confirmation(tmp_path):
    from luna_agent.tools.entry import ToolEntry
    from luna_agent.tools.executor import execute_tool_call_result
    from luna_agent.tools.registry import tool_registry

    calls = 0

    async def handler():
        nonlocal calls
        calls += 1
        return "ok"

    async def unexpected_confirm(_decision):
        raise AssertionError("Local Auto should not confirm a default cached tool")

    entry = ToolEntry("local_auto_cached_demo", "demo", {}, handler, approval_mode="cached")
    tool_registry.register(entry)
    try:
        result = await execute_tool_call_result(
            {"id": "local-auto", "name": entry.name, "input": {}},
            agent=_agent(tmp_path, mode="local-auto"),
            confirm=unexpected_confirm,
        )
    finally:
        tool_registry.unregister(entry.name)

    assert result.status == "success"
    assert calls == 1


@pytest.mark.asyncio
async def test_local_auto_preserves_explicit_prompt_override(tmp_path):
    from luna_agent.tools.entry import ToolEntry
    from luna_agent.tools.executor import execute_tool_call_result
    from luna_agent.tools.registry import tool_registry

    confirmations = 0

    async def handler():
        return "ok"

    async def confirm(_decision):
        nonlocal confirmations
        confirmations += 1
        return "allow"

    entry = ToolEntry("local_auto_prompt_demo", "demo", {}, handler, approval_mode="prompt")
    tool_registry.register(entry)
    try:
        result = await execute_tool_call_result(
            {"id": "local-auto-prompt", "name": entry.name, "input": {}},
            agent=_agent(tmp_path, mode="local-auto"),
            confirm=confirm,
        )
    finally:
        tool_registry.unregister(entry.name)

    assert result.status == "success"
    assert confirmations == 1


@pytest.mark.asyncio
async def test_nested_tool_call_inherits_confirm_and_cached_grant(tmp_path):
    import luna_agent.plugins.builtin.tools.bridge.bridge  # noqa: F401

    from luna_agent.tools.entry import ToolEntry
    from luna_agent.tools.executor import execute_tool_call_result
    from luna_agent.tools.registry import tool_registry

    calls = 0
    confirmations = 0

    async def handler(value: str):
        nonlocal calls
        calls += 1
        return f"nested:{value}"

    async def confirm(_decision):
        nonlocal confirmations
        confirmations += 1
        return "always"

    entry = ToolEntry(
        "nested_cached_demo",
        "demo",
        {"type": "object", "properties": {"value": {"type": "string"}}},
        handler,
        approval_mode="cached",
        idempotent=False,
    )
    tool_registry.register(entry)
    agent = _agent(tmp_path)
    try:
        first = await execute_tool_call_result(
            {
                "id": "outer-one",
                "name": "tool_call",
                "input": {"name": entry.name, "arguments": {"value": "one"}},
            },
            agent=agent,
            confirm=confirm,
        )
        second = await execute_tool_call_result(
            {
                "id": "outer-two",
                "name": "tool_call",
                "input": {"name": entry.name, "arguments": {"value": "two"}},
            },
            agent=agent,
            confirm=confirm,
        )
    finally:
        tool_registry.unregister(entry.name)

    assert first.status == second.status == "success"
    assert first.content == "nested:one"
    assert second.content == "nested:two"
    assert calls == 2
    assert confirmations == 1


@pytest.mark.asyncio
async def test_nested_tool_call_preserves_authorization_denial(tmp_path):
    import luna_agent.plugins.builtin.tools.bridge.bridge  # noqa: F401

    from luna_agent.tools.entry import ToolEntry
    from luna_agent.tools.executor import execute_tool_call_result
    from luna_agent.tools.registry import tool_registry

    called = False

    async def handler():
        nonlocal called
        called = True
        return "should not run"

    async def deny(_decision):
        return "deny"

    entry = ToolEntry(
        "nested_denied_demo",
        "demo",
        {"type": "object", "properties": {}},
        handler,
        approval_mode="cached",
        idempotent=False,
    )
    tool_registry.register(entry)
    try:
        result = await execute_tool_call_result(
            {
                "id": "outer-denied",
                "name": "tool_call",
                "input": {"name": entry.name, "arguments": {}},
            },
            agent=_agent(tmp_path),
            confirm=deny,
        )
    finally:
        tool_registry.unregister(entry.name)

    assert result.status == "denied"
    assert result.category == "authorization"
    assert result.reason_code == "security_approval_required"
    assert result.permission_decision == "ask"
    assert called is False


@pytest.mark.asyncio
async def test_prompt_tool_approval_never_persists(tmp_path):
    from luna_agent.tools.entry import ToolEntry
    from luna_agent.tools.executor import execute_tool_call_result
    from luna_agent.tools.registry import tool_registry

    confirmations = 0

    async def handler():
        return "ok"

    async def confirm(_decision):
        nonlocal confirmations
        confirmations += 1
        return "always"

    entry = ToolEntry("prompt_demo", "demo", {}, handler, approval_mode="prompt")
    tool_registry.register(entry)
    agent = _agent(tmp_path)
    try:
        first = await execute_tool_call_result(
            {"id": "one", "name": entry.name, "input": {}}, agent=agent, confirm=confirm
        )
        second = await execute_tool_call_result(
            {"id": "two", "name": entry.name, "input": {}}, agent=agent, confirm=confirm
        )
    finally:
        tool_registry.unregister(entry.name)

    assert first.status == second.status == "success"
    assert confirmations == 2


def test_tool_and_mcp_server_approval_overrides(tmp_path):
    from luna_agent.security.evaluator import evaluate_tool_security, prepare_tool_call
    from luna_agent.security.session import SecurityStateStore
    from luna_agent.tools.entry import ToolEntry

    settings = SimpleNamespace(
        execution_mode="ask-first",
        sandbox_roots=[tmp_path],
        permission_grant_ttl_minutes=60,
        tool_approval_config={
            "tools": {"local_demo": "deny"},
            "mcp_servers": {"github": "prompt"},
        },
    )
    context = SecurityStateStore(settings).context("session-a")
    local = ToolEntry("local_demo", "demo", {}, lambda: None)
    github = ToolEntry("mcp__github__issues", "demo", {}, lambda: None, approval_mode="cached")

    local_decision = evaluate_tool_security(
        prepare_tool_call({"name": local.name, "input": {}}, local), context
    )
    github_decision = evaluate_tool_security(
        prepare_tool_call({"name": github.name, "input": {}}, github), context
    )

    assert local_decision.reason_code == "tool_approval_denied"
    assert github_decision.tool_approval_mode == "prompt"


def test_local_auto_keeps_explicit_cached_override(tmp_path):
    from luna_agent.security.evaluator import evaluate_tool_security, prepare_tool_call
    from luna_agent.security.session import SecurityStateStore
    from luna_agent.tools.entry import ToolEntry

    settings = SimpleNamespace(
        execution_mode="local-auto",
        sandbox_roots=[tmp_path],
        sandbox_read_roots=[],
        permission_grant_ttl_minutes=60,
        tool_approval_config={"tools": {"external_demo": "cached"}},
    )
    context = SecurityStateStore(settings).context("session-a")
    entry = ToolEntry("external_demo", "demo", {}, lambda: None, approval_mode="cached")

    decision = evaluate_tool_security(
        prepare_tool_call({"name": entry.name, "input": {}}, entry), context
    )

    assert decision.decision == "ask"
    assert decision.tool_approval_mode == "cached"


def test_bash_declares_working_directory_and_network_resources(tmp_path, monkeypatch):
    from luna_agent.plugins.builtin.tools.builtin import bash

    monkeypatch.setattr(bash, "_work_dir", tmp_path.resolve())

    local = bash.resource_requirements({"command": "ls -la"})
    remote = bash.resource_requirements(
        {"command": "curl https://api.github.com/repos/openai/codex"}
    )

    assert [item.as_dict() for item in local] == [
        {
            "kind": "filesystem",
            "resource": str(tmp_path.resolve()),
            "access": "write",
            "reason": "bash working directory",
        }
    ]
    assert remote[1].resource == "https://api.github.com:443"
    assert remote[1].access == "connect"


@pytest.mark.asyncio
async def test_unscoped_external_tool_fails_closed():
    from luna_agent.tools.entry import ToolEntry
    from luna_agent.tools.executor import execute_tool_call_result
    from luna_agent.tools.registry import tool_registry

    async def handler():
        return "should not run"

    entry = ToolEntry("unscoped_cached_demo", "demo", {}, handler, approval_mode="cached")
    tool_registry.register(entry)
    try:
        result = await execute_tool_call_result(
            {"id": "unscoped", "name": entry.name, "input": {}}
        )
    finally:
        tool_registry.unregister(entry.name)

    assert result.status == "denied"
    assert result.reason_code == "resource_permission_denied"


@pytest.mark.asyncio
async def test_resource_approval_is_enforced_by_file_boundary(tmp_path):
    from luna_agent.security.models import ResourceRequirement
    from luna_agent.tools.entry import ToolEntry
    from luna_agent.tools.executor import execute_tool_call_result
    from luna_agent.tools.registry import tool_registry
    from luna_agent.tools.sandbox import get_sandbox, init_sandbox

    init_sandbox([tmp_path], [])
    target = tmp_path.parent / f"{tmp_path.name}-approved.txt"

    async def handler(path: str):
        full = Path(path).resolve()
        error = get_sandbox().check_path(full, access="write")
        if error:
            return error
        full.write_text("ok", encoding="utf-8")
        return "written"

    entry = ToolEntry(
        "resource_write_demo",
        "demo",
        {},
        handler,
        resource_resolver=lambda inp: [
            ResourceRequirement("filesystem", str(Path(inp["path"]).resolve()), "write")
        ],
    )
    tool_registry.register(entry)
    agent = _agent(tmp_path, mode="ask-first")
    try:
        async def approve(_decision):
            return "always"

        result = await execute_tool_call_result(
            {"id": "write", "name": entry.name, "input": {"path": str(target)}},
            agent=agent,
            confirm=approve,
        )
    finally:
        tool_registry.unregister(entry.name)

    assert result.status == "success"
    assert target.read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_hook_arguments_are_evaluated_after_modification(tmp_path):
    from luna_agent.hooks import HookEvent, HookManager, PreToolUseOutcome
    from luna_agent.security.models import ResourceRequirement
    from luna_agent.tools.entry import ToolEntry
    from luna_agent.tools.executor import execute_tool_call_result
    from luna_agent.tools.registry import tool_registry

    original = tmp_path / "original.txt"
    modified = tmp_path / "modified.txt"
    seen = []

    async def handler(path: str):
        seen.append(path)
        return "ok"

    entry = ToolEntry(
        "hook_resource_demo",
        "demo",
        {},
        handler,
        resource_resolver=lambda inp: [
            ResourceRequirement("filesystem", str(Path(inp["path"]).resolve()), "write")
        ],
    )
    tool_registry.register(entry)
    decisions = []

    async def deny(decision):
        decisions.append(decision)
        return "deny"

    agent = _agent(tmp_path)
    agent._hook_manager = HookManager()
    agent._hook_turn_id = "hook-turn"
    agent._hook_source = None
    agent._hook_additional_contexts = []
    agent._memory_session_key = "test"
    agent._hook_manager.register(
        owner="test",
        event=HookEvent.PRE_TOOL_USE,
        matcher="hook_resource_demo",
        callback=lambda event: PreToolUseOutcome(
            updated_input={"path": str(modified)}
        ),
    )
    try:
        result = await execute_tool_call_result(
            {"id": "hook", "name": entry.name, "input": {"path": str(original)}},
            agent=agent,
            confirm=deny,
        )
    finally:
        tool_registry.unregister(entry.name)

    assert result.status == "denied"
    assert seen == []
    assert decisions[0].requested_resources[0]["resource"] == str(modified)
