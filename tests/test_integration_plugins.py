from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent.config import Settings
from personal_agent.hooks import HookEnvelope, HookEvent, HookManager, HookScope
from personal_agent.plugins import PluginManager, PluginStatus


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins"


def _manager(tmp_path, monkeypatch, key: str, config: dict):
    monkeypatch.setenv("GITHUB_MCP_AUTH", "Bearer test-token")
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[PLUGIN_ROOT],
        plugins_enabled=[key],
        plugins_disabled=[
            item for item in (
                "integrations/codex-bridge",
                "integrations/github-assistant",
                "integrations/developer-docs",
                "integrations/browser-operator",
            ) if item != key
        ],
        plugins_config={key: config},
        mcp_enabled=False,
        memory_external_provider="none",
    )
    manager = PluginManager(
        settings,
        plugin_dirs=[PLUGIN_ROOT],
        state_path=tmp_path / "plugin-state.json",
        include_builtin=False,
        hook_manager=HookManager(),
    )
    manager.load_enabled()
    return manager


def test_github_assistant_registers_mcp_skills_command_and_hook(tmp_path, monkeypatch):
    key = "integrations/github-assistant"
    manager = _manager(tmp_path, monkeypatch, key, {
        "repositories": ["openai/codex"],
        "write_enabled": False,
    })
    plugin = next(item for item in manager.list_plugins() if item.key == key)

    assert plugin.status == PluginStatus.LOADED
    assert [item.name for item in manager.get_mcp_servers()] == ["github"]
    assert set(plugin.skills_registered) == {
        "repo-summary", "review-pr", "triage-issues", "release-notes",
    }
    assert manager.get_command("github-status", scope="cli") is not None
    assert len(manager.hook_manager.registrations(HookEvent.PRE_TOOL_USE)) == 1
    manager.unload_plugin(key)


@pytest.mark.asyncio
async def test_github_assistant_blocks_writes_and_unlisted_repositories(tmp_path, monkeypatch):
    key = "integrations/github-assistant"
    manager = _manager(tmp_path, monkeypatch, key, {
        "repositories": ["openai/codex"],
        "write_enabled": False,
    })

    write = await manager.hook_manager.dispatch(_tool_event(
        "mcp__github__create_issue",
        {"owner": "openai", "repo": "codex", "title": "x"},
    ))
    issue_write = await manager.hook_manager.dispatch(_tool_event(
        "mcp__github__issue_write",
        {"method": "create", "owner": "openai", "repo": "codex", "title": "x"},
    ))
    review_write = await manager.hook_manager.dispatch(_tool_event(
        "mcp__github__pull_request_review_write",
        {"method": "create", "owner": "openai", "repo": "codex", "pullNumber": 1},
    ))
    outside = await manager.hook_manager.dispatch(_tool_event(
        "mcp__github__get_file_contents",
        {"owner": "other", "repo": "project"},
    ))
    allowed = await manager.hook_manager.dispatch(_tool_event(
        "mcp__github__get_file_contents",
        {"owner": "openai", "repo": "codex"},
    ))

    assert write.blocked and "write operations" in write.reason
    assert issue_write.blocked and "write operations" in issue_write.reason
    assert review_write.blocked and "write operations" in review_write.reason
    assert outside.blocked and "allowlist" in outside.reason
    assert allowed.blocked is False
    manager.unload_plugin(key)


@pytest.mark.asyncio
async def test_github_policy_applies_to_lazily_registered_mcp_tool(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.executor import execute_tool_call_result
    from personal_agent.tools.registry import tool_registry

    key = "integrations/github-assistant"
    manager = _manager(tmp_path, monkeypatch, key, {
        "repositories": ["owner/repo"],
        "write_enabled": False,
    })
    called = False

    async def create_issue(owner: str, repo: str):
        nonlocal called
        called = True
        return "created"

    name = "mcp__github__create_issue"
    tool_registry.register(ToolEntry(
        name=name,
        description="late GitHub MCP write tool",
        schema={"type": "object", "properties": {}},
        handler=create_issue,
    ))
    agent = SimpleNamespace(
        _hook_manager=manager.hook_manager,
        _hook_turn_id="late-tool",
        _hook_source=None,
        _hook_additional_contexts=[],
        _memory_session_key="test",
        _security_context=None,
        _interrupt_requested=False,
        _tool_calls_this_turn=0,
        _max_tool_calls_per_turn=10,
        _destructive_calls_this_turn=0,
        _max_destructive_per_turn=3,
    )
    try:
        result = await execute_tool_call_result(
            {
                "id": "late-write",
                "name": name,
                "input": {"owner": "owner", "repo": "repo"},
            },
            agent=agent,
        )
    finally:
        tool_registry.unregister(name)
        manager.unload_plugin(key)

    assert result.status == "denied"
    assert result.category == "hook"
    assert "write operations" in result.error
    assert called is False


def test_developer_docs_registers_context7_and_skills(tmp_path, monkeypatch):
    key = "integrations/developer-docs"
    manager = _manager(tmp_path, monkeypatch, key, {})
    plugin = next(item for item in manager.list_plugins() if item.key == key)

    assert plugin.status == PluginStatus.LOADED
    assert [item.name for item in manager.get_mcp_servers()] == ["context7"]
    assert set(plugin.skills_registered) == {
        "library-docs", "upgrade-library", "compare-library-api",
    }
    assert manager.get_command("developer-docs-status", scope="cli") is not None
    manager.unload_plugin(key)


@pytest.mark.asyncio
async def test_browser_operator_registers_playwright_and_enforces_policy(tmp_path, monkeypatch):
    key = "integrations/browser-operator"
    manager = _manager(tmp_path, monkeypatch, key, {
        "allowed_domains": ["example.com"],
        "allow_file_upload": False,
        "allow_code_execution": False,
    })
    plugin = next(item for item in manager.list_plugins() if item.key == key)

    assert plugin.status == PluginStatus.LOADED
    server = manager.get_mcp_servers()[0]
    assert server.name == "playwright"
    assert "--browser" not in server.args
    if "--executable-path" in server.args:
        executable = Path(server.args[server.args.index("--executable-path") + 1])
        assert executable.is_file()
    assert set(plugin.skills_registered) == {
        "inspect-web-page", "test-web-page", "operate-web-page",
    }
    outside = await manager.hook_manager.dispatch(_tool_event(
        "mcp__playwright__browser_navigate", {"url": "https://other.test"},
    ))
    upload = await manager.hook_manager.dispatch(_tool_event(
        "mcp__playwright__browser_file_upload", {"paths": ["a.txt"]},
    ))
    allowed = await manager.hook_manager.dispatch(_tool_event(
        "mcp__playwright__browser_navigate", {"url": "https://docs.example.com/page"},
    ))

    assert outside.blocked and "allowlist" in outside.reason
    assert upload.blocked and "uploads" in upload.reason
    assert allowed.blocked is False
    status = await manager.execute_command("browser-status", scope="cli")
    assert "readiness:" in status
    manager.unload_plugin(key)


def _tool_event(tool_name: str, tool_input: dict) -> HookEnvelope:
    return HookEnvelope(
        event_name=HookEvent.PRE_TOOL_USE,
        scope=HookScope.TURN,
        payload={"tool_name": tool_name, "tool_input": tool_input},
    )
