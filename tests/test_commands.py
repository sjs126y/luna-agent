"""Shared slash command service."""

from __future__ import annotations

import pytest

from personal_agent.commands.registry import command_specs_as_dict
from personal_agent.commands.runtime import (
    handle_slash_command,
    slash_argument_choices,
)
from personal_agent.config import Settings
from personal_agent.models.messages import SessionSource
from personal_agent.plugins.models import CommandEntry


class Agent:
    session_api_calls = 2
    session_prompt_tokens = 10
    session_completion_tokens = 5
    _last_skill_summaries = ""
    _last_skill_injection = ""
    _last_memory_injections = ""
    _tool_calls_this_turn = 0
    _max_tool_calls_per_turn = 20
    _interrupt_requested = False
    _cached_system_prompt = "system"
    tools = []
    model = "deepseek-chat"
    _memory_manager = None
    _security_context = None
    _security_grant_ttl_seconds = 3600

    class Provider:
        model = "deepseek-chat"
        context_window = 1000

    _provider = Provider()

class PluginManager:
    def __init__(self):
        self.commands = {}

    def get_command(self, name, *, scope="slash"):
        entry = self.commands.get(name)
        if entry is None:
            return None
        if entry.scope not in {scope, "both"}:
            return None
        return entry

    async def execute_command(self, name, **kwargs):
        value = self.commands[name].handler(**kwargs)
        if hasattr(value, "__await__"):
            value = await value
        return value


class QueryService:
    async def list_sessions(self, *, platform: str, user_id: str, current_key: str, limit: int = 10):
        return {
            "platform": platform,
            "user_id": user_id,
            "current_key": current_key,
            "limit": limit,
            "total": 2,
            "items": [
                {"session_key": f"{platform}:default:{user_id}", "message_count": 3},
                {"session_key": f"{platform}:work:{user_id}", "message_count": 8},
            ][:limit],
        }


class ConversationService:
    queries = QueryService()


class Runtime:
    def __init__(self, tmp_path):
        self.settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
        self.plugin_manager = PluginManager()
        self.conversation_service = ConversationService()
        self._session_key = "cli:default:local"
        self.source = SessionSource(platform="cli", user_id="local", chat_id="default")
        self.agent = Agent()
        from personal_agent.security.session import SecurityStateStore

        self.security_store = SecurityStateStore(self.settings)
        self.agent._security_context = self.security_store.context(self._session_key)
        self.reset_called = False
        self.clear_called = False
        self.switched_to = ""
        self.renamed_to = ""
        self.deleted = None
        self.exported = False
        self.memory_deleted = None
        self.running = False
        self.steers = []
        self.tool_runs = [
            {
                "id": 1,
                "session_id": "session-a",
                "session_key": "cli:default:local",
                "turn_id": "turn-1",
                "tool_use_id": "call-1",
                "tool_name": "read",
                "status": "success",
                "category": "read",
                "duration": 0.1,
                "input_summary": '{"path": "README.md"}',
                "output_summary": "README",
                "full_output": "README contents",
                "output_truncated": False,
                "error": "",
                "permission_category": "read",
                "permission_decision": "allow",
                "execution_mode": "standard",
                "created_at": 1.0,
            },
            {
                "id": 2,
                "session_id": "session-b",
                "session_key": "cli:other:local",
                "turn_id": "turn-2",
                "tool_use_id": "call-2",
                "tool_name": "bash",
                "status": "denied",
                "category": "authorization",
                "duration": 0.0,
                "input_summary": '{"cmd": "rm"}',
                "output_summary": "",
                "full_output": "",
                "output_truncated": False,
                "error": "blocked",
                "permission_category": "bash",
                "permission_decision": "deny",
                "execution_mode": "standard",
                "created_at": 2.0,
            },
        ]

    @property
    def session_key(self):
        return self._session_key

    async def get_agent(self):
        return self.agent

    async def reset_session(self):
        self.reset_called = True

    async def switch_session(self, name: str):
        self.switched_to = name
        self._session_key = f"cli:{name}:local"
        return f"会话已切换: {self._session_key}"

    async def list_sessions(self):
        return f"当前会话: {self._session_key}"

    async def current_session(self):
        return f"当前会话: {self._session_key}\nsession id: abc123\n消息数: 0"

    async def rename_session(self, name: str):
        self.renamed_to = name
        self._session_key = f"cli:{name}:local"
        return f"会话已重命名: {self._session_key}"

    async def delete_session(self, name: str | None = None):
        self.deleted = name
        return f"会话已删除: {name or self._session_key}"

    async def load_history(self):
        return [{
            "role": "user",
            "content": [{"type": "text", "text": "hello"}],
        }]

    async def export_session(self):
        self.exported = True
        return 1, "/tmp/export.jsonl"

    async def memory_report(self):
        return {
            "providers": {
                "builtin": {"provider": "internal_markdown", "available": True, "entries": 1},
                "external": {"provider": "", "available": False, "entries": 0},
            },
            "last_errors": {},
        }

    async def memory_entries(self, *, target: str = "all"):
        return [{"id": "memory:1", "provider": "builtin", "target": "memory", "text": f"hello {target}"}]

    async def memory_search(self, query: str, *, target: str = "all"):
        return [{"id": "memory:1", "provider": "builtin", "target": "memory", "text": query}]

    async def memory_entry(self, identifier: str, *, target: str = "all"):
        if identifier == "memory:1":
            return {"id": identifier, "provider": "builtin", "target": target, "text": "hello"}
        return None

    async def memory_delete(self, identifier: str, *, target: str = "all"):
        self.memory_deleted = (identifier, target)
        return identifier == "memory:1"

    async def tool_runs_recent(self, *, limit: int = 10, all_sessions: bool = False):
        items = self.tool_runs if all_sessions else [
            item for item in self.tool_runs if item["session_key"] == self.session_key
        ]
        return {
            "scope": "all" if all_sessions else "session",
            "session_key": "" if all_sessions else self.session_key,
            "limit": limit,
            "items": items[:limit],
        }

    async def tool_run_detail(self, run_id: int):
        for item in self.tool_runs:
            if item["id"] == run_id:
                return item
        return None

    async def tool_runs_summary(self, *, limit: int = 50, all_sessions: bool = False):
        items = self.tool_runs if all_sessions else [
            item for item in self.tool_runs if item["session_key"] == self.session_key
        ]
        items = items[:limit]
        tool_counts = {}
        status_counts = {}
        category_counts = {}
        for item in items:
            tool_counts[item["tool_name"]] = tool_counts.get(item["tool_name"], 0) + 1
            status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
            category_counts[item["category"]] = category_counts.get(item["category"], 0) + 1
        return {
            "scope": "all" if all_sessions else "session",
            "session_key": "" if all_sessions else self.session_key,
            "limit": limit,
            "inspected": len(items),
            "tool_counts": dict(sorted(tool_counts.items())),
            "status_counts": dict(sorted(status_counts.items())),
            "category_counts": dict(sorted(category_counts.items())),
            "denied": status_counts.get("denied", 0),
            "failed": status_counts.get("error", 0),
            "timeouts": status_counts.get("timeout", 0),
            "truncated": sum(1 for item in items if item.get("output_truncated")),
        }

    async def clear_agent(self):
        self.clear_called = True

    async def is_session_running(self) -> bool:
        return self.running

    async def add_steer(self, text: str) -> str:
        if not self.running:
            return "当前没有运行中的任务可修正。"
        self.steers.append(text)
        return f"已收到，会在当前任务下一步应用。（test-steer）"

    async def set_mode(self, mode: str) -> str:
        from personal_agent.security.modes import mode_preset

        state = self.security_store.set_mode(self.session_key, mode)
        self.agent._security_context = self.security_store.context(self.session_key)
        return f"执行模式已切换: {mode_preset(state.mode_id).label}。"

    async def clear_security_grants(self) -> bool:
        context = self.agent._security_context
        changed = bool(context.state.tool_grants or context.state.resource_grants)
        context.state.clear_grants()
        return changed

    def plugin_command_kwargs(self, args: str):
        return {
            "args": args,
            "runtime": self,
            "session_key": self.session_key,
        }


@pytest.mark.asyncio
async def test_shared_command_core_session_usage_export_and_permissions(tmp_path):
    runtime = Runtime(tmp_path)

    result = await handle_slash_command(runtime, "/new")
    assert result.handled
    assert runtime.reset_called
    assert runtime.clear_called

    result = await handle_slash_command(runtime, "/session work")
    assert result.response == "会话已切换: cli:work:local"

    result = await handle_slash_command(runtime, "/session list")
    assert "当前会话: cli:work:local" in result.response

    result = await handle_slash_command(runtime, "/session current")
    assert "session id" in result.response

    result = await handle_slash_command(runtime, "/session switch ops")
    assert result.response == "会话已切换: cli:ops:local"

    result = await handle_slash_command(runtime, "/session rename renamed")
    assert "会话已重命名" in result.response
    assert runtime.renamed_to == "renamed"

    result = await handle_slash_command(runtime, "/session delete current")
    assert "会话已删除" in result.response
    assert runtime.deleted is None

    result = await handle_slash_command(runtime, "/session delete old")
    assert "会话已删除" in result.response
    assert runtime.deleted == "old"

    result = await handle_slash_command(runtime, "/usage")
    assert "上下文窗口" in result.response
    assert "最近一轮工具执行" in result.response
    assert "单轮工具上限" in result.response
    assert "本轮工具调用" not in result.response

    result = await handle_slash_command(runtime, "/export")
    assert "已导出 1 条对话" in result.response
    assert runtime.exported

    result = await handle_slash_command(runtime, "/allow write")
    assert result.handled is False

    result = await handle_slash_command(runtime, "/permissions")
    assert "临时授权 TTL" in result.response
    assert result.payload["tool_grants"] == []
    assert result.payload["resource_grants"] == []

    result = await handle_slash_command(runtime, "/deny write")
    assert result.response == "用法: /deny all"

    result = await handle_slash_command(runtime, "/memory list")
    assert result.handled
    assert "记忆列表: 1 条" in result.response
    assert "memory:1" in result.response

    result = await handle_slash_command(runtime, "/memory search needle --target=memory")
    assert "记忆搜索结果: 1 条" in result.response
    assert "needle" in result.response

    result = await handle_slash_command(runtime, "/memory search needle -t memory")
    assert "needle memory" not in result.response

    result = await handle_slash_command(runtime, "/memory show memory:1")
    assert "记忆: memory:1" in result.response

    result = await handle_slash_command(runtime, "/memory delete memory:1 -t external")
    assert result.response == "已删除记忆: memory:1"
    assert runtime.memory_deleted == ("memory:1", "external")

    result = await handle_slash_command(runtime, "/memory doctor")
    assert "Memory 诊断" in result.response


@pytest.mark.asyncio
async def test_removed_allow_command_is_not_handled(tmp_path):
    runtime = Runtime(tmp_path)
    result = await handle_slash_command(runtime, "/allow network")

    assert result.handled is False


@pytest.mark.asyncio
async def test_mode_command_switches_security_context_and_reports(tmp_path):
    runtime = Runtime(tmp_path)

    result = await handle_slash_command(runtime, "/mode")
    assert "当前模式: Ask First" in result.response
    assert result.kind == "mode"
    assert result.payload["current"]["label"] == "Ask First"

    result = await handle_slash_command(runtime, "/mode list")
    assert "Read Only" in result.response
    assert "Full Auto" in result.response
    assert {item["slug"] for item in result.payload["modes"]} >= {"read-only", "full-auto"}

    result = await handle_slash_command(runtime, "/mode set Local Auto")
    assert "Local Auto" in result.response
    assert runtime.agent._security_context.mode_id == "local-auto"
    assert result.payload["selected"]["profile"] == "workspace"

    result = await handle_slash_command(runtime, "/mode acceptEdits")
    assert result.payload["error"] == "unknown_mode"

    result = await handle_slash_command(runtime, "/mode")
    assert "当前模式: Local Auto" in result.response

    result = await handle_slash_command(runtime, "/mode Full Auto")
    assert "Full Auto" in result.response
    assert runtime.agent._security_context.mode_id == "full-auto"

    result = await handle_slash_command(runtime, "/mode Read Only")
    assert "Read Only" in result.response
    assert runtime.agent._security_context.mode_id == "read-only"

    result = await handle_slash_command(runtime, "/mode normal")
    assert result.payload["error"] == "unknown_mode"

    result = await handle_slash_command(runtime, "/mode bogus")
    assert "用法" in result.response
    assert result.error
    assert result.payload["error"] == "unknown_mode"

@pytest.mark.asyncio
async def test_shared_command_stop_plugin_skill_and_unhandled(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)

    result = await handle_slash_command(runtime, "/stop")
    assert result.response == "已停止。"
    assert runtime.agent._interrupt_requested

    async def plugin_handler(args="", **kwargs):
        return f"plugin:{args}:{kwargs['session_key']}"

    runtime.plugin_manager.commands["demo"] = CommandEntry(
        name="demo",
        description="demo",
        handler=plugin_handler,
    )
    result = await handle_slash_command(runtime, "/demo hi")
    assert result.response == "plugin:hi:cli:default:local"

    runtime.plugin_command_scopes = ("cli", "slash")
    runtime.plugin_manager.commands["local"] = CommandEntry(
        name="local",
        description="local command",
        handler=plugin_handler,
        scope="cli",
    )
    result = await handle_slash_command(runtime, "/local only")
    assert result.response == "plugin:only:cli:default:local"

    result = await handle_slash_command(runtime, "/help")
    assert "/commands - 列出 slash commands" in result.response
    assert "/tools - 查看可用工具" in result.response
    assert "/permissions - 查看当前权限策略和 grants" in result.response
    assert "/local - local command" in result.response
    assert "/demo - demo" in result.response

    result = await handle_slash_command(runtime, "/commands")
    assert "/mode - 查看或切换执行模式" in result.response
    assert "/protocol - 查看前端事件协议摘要" in result.response
    assert result.kind == "commands"
    assert result.payload["version"] == 1

    result = await handle_slash_command(runtime, "/commands mode")
    assert "子命令:" in result.response
    assert "/mode set <mode>" in result.response
    assert result.payload["command"]["name"] == "mode"
    assert result.payload["command"]["mutates_state"] is True

    result = await handle_slash_command(runtime, "/commands missing")
    assert result.error
    assert result.payload["error"] == "unknown_command"

    result = await handle_slash_command(runtime, "/commands json")
    assert '"version": 1' in result.response
    assert '"name": "mode"' in result.response
    assert '"name": "local"' in result.response
    assert result.payload["version"] == 1
    assert any(item["name"] == "local" for item in result.payload["plugin_commands"])

    from personal_agent.skills.registry import skill_registry

    original_load = skill_registry.load
    monkeypatch.setattr(skill_registry, "load", lambda name: "skill body")
    try:
        result = await handle_slash_command(runtime, "/python-expert fix it")
    finally:
        monkeypatch.setattr(skill_registry, "load", original_load)
    assert result.continue_text == "fix it"
    assert "[技能: python-expert]" in runtime.agent._pending_skill_injection

    monkeypatch.setattr(skill_registry, "load", lambda name: "")
    result = await handle_slash_command(runtime, "/unknown")
    assert not result.handled

    result = await handle_slash_command(runtime, "/permission")
    assert result.handled
    assert result.error
    assert "/permissions" in result.suggestions


@pytest.mark.asyncio
async def test_steer_command_delegates_to_runtime(tmp_path):
    runtime = Runtime(tmp_path)

    result = await handle_slash_command(runtime, "/steer")
    assert result.handled
    assert "用法" in result.response

    result = await handle_slash_command(runtime, "/steer 回答短一点")
    assert result.response == "当前没有运行中的任务可修正。"
    assert runtime.steers == []

    runtime.running = True
    result = await handle_slash_command(runtime, "/steer 回答短一点")

    assert "已收到" in result.response
    assert runtime.steers == ["回答短一点"]


@pytest.mark.asyncio
async def test_shared_command_tools_permissions_and_protocol(tmp_path):
    import personal_agent.plugins.builtin.tools.builtin.file_read  # noqa: F401

    runtime = Runtime(tmp_path)

    result = await handle_slash_command(runtime, "/tools")
    assert "工具总览" in result.response
    assert "by permission:" in result.response
    assert result.kind == "tools"
    assert result.payload["summary"]["total"] >= 1

    result = await handle_slash_command(runtime, "/tools show read")
    assert "工具: read" in result.response
    assert "permission:" in result.response
    assert "risk:" in result.response
    assert result.payload["tool"]["name"] == "read"
    assert "input_properties" in result.payload["tool"]

    result = await handle_slash_command(runtime, "/tools show missing_tool")
    assert "未找到工具: missing_tool" in result.response
    assert result.payload["error"] == "unknown_tool"

    result = await handle_slash_command(runtime, "/tools shwo")
    assert result.payload["error"] == "unknown_tools_action"
    assert "/tools show" in result.suggestions

    result = await handle_slash_command(runtime, "/permissions")
    assert "权限策略: Ask First" in result.response
    assert "approval policy: on-request" in result.response
    assert result.kind == "permissions"
    assert result.payload["execution_mode"] == "Ask First"
    assert result.payload["tool_grants"] == []

    result = await handle_slash_command(runtime, "/permissions grants")
    assert result.response == "当前会话授权:\n- 无"
    assert result.payload["tool_grants"] == []

    result = await handle_slash_command(runtime, "/protocol")
    assert "事件协议" in result.response
    assert "version: 1" in result.response
    assert "完整 schema: personal-agent protocol schema --json" in result.response
    assert result.kind == "protocol"
    assert result.payload["protocol_version"] == 1
    assert result.payload["event_count"] >= 1

    result = await handle_slash_command(runtime, "/protocol schema")
    assert "event names:" in result.response
    assert "tool_decision" in result.response
    assert "tool_decision" in result.payload["events"]


@pytest.mark.asyncio
async def test_security_grants_clear_without_category_compatibility(tmp_path):
    runtime = Runtime(tmp_path)
    context = runtime.agent._security_context
    context.state.grant_tool("core:bash", ttl_seconds=60)

    allowed = await handle_slash_command(runtime, "/allow write")
    denied = await handle_slash_command(runtime, "/deny all")

    assert allowed.handled is False
    assert "已撤销当前会话" in denied.response
    assert context.state.tool_grants == {}


@pytest.mark.asyncio
async def test_shared_command_tool_runs_queries(tmp_path):
    runtime = Runtime(tmp_path)

    result = await handle_slash_command(runtime, "/tool-runs")
    assert result.kind == "tool_runs"
    assert "工具运行记录: 1 条" in result.response
    assert result.payload["action"] == "recent"
    assert result.payload["scope"] == "session"
    assert result.payload["items"][0]["tool_name"] == "read"

    result = await handle_slash_command(runtime, "/tool-runs recent --all --limit 20")
    assert result.payload["scope"] == "all"
    assert [item["tool_name"] for item in result.payload["items"]] == ["read", "bash"]

    result = await handle_slash_command(runtime, "/tool-runs summary")
    assert "工具运行摘要" in result.response
    assert result.payload["inspected"] == 1
    assert result.payload["tool_counts"] == {"read": 1}

    result = await handle_slash_command(runtime, "/tool-runs summary --all")
    assert result.payload["inspected"] == 2
    assert result.payload["status_counts"] == {"denied": 1, "success": 1}

    result = await handle_slash_command(runtime, "/tool-runs show 1")
    assert "Tool Run #1" in result.response
    assert result.payload["tool_run"]["full_output"] == "README contents"

    result = await handle_slash_command(runtime, "/tool-runs show missing")
    assert result.error
    assert result.payload["error"] == "invalid_tool_run_id"

    result = await handle_slash_command(runtime, "/tool-runs recnt")
    assert result.error
    assert result.payload["error"] == "unknown_tool_runs_action"
    assert "/tool-runs recent" in result.suggestions


def test_command_registry_exports_structured_metadata(tmp_path):
    runtime = Runtime(tmp_path)
    data = command_specs_as_dict(runtime)

    assert data["version"] == 1
    names = {item["name"] for item in data["commands"]}
    assert {"commands", "tools", "tool-runs", "permissions", "protocol"} <= names
    mode = next(item for item in data["commands"] if item["name"] == "mode")
    assert {child["name"] for child in mode["children"]} >= {"list", "show", "set"}
    assert mode["mutates_state"] is True
    assert mode["requires_agent"] is True
    mode_set = next(child for child in mode["children"] if child["name"] == "set")
    mode_argument = mode_set["arguments"][0]
    assert mode_argument["name"] == "mode"
    assert mode_argument["kind"] == "choice"
    assert [choice["value"] for choice in mode_argument["choices"]] == [
        "Read Only",
        "Ask First",
        "Local Auto",
        "Full Auto",
    ]
    assert "allow" not in names
    deny = next(item for item in data["commands"] if item["name"] == "deny")
    assert [choice["value"] for choice in deny["arguments"][0]["choices"]] == ["all"]
    tools = next(item for item in data["commands"] if item["name"] == "tools")
    tools_show = next(child for child in tools["children"] if child["name"] == "show")
    assert tools_show["arguments"][0]["kind"] == "dynamic"
    assert tools_show["arguments"][0]["provider"] == "tools"
    session = next(item for item in data["commands"] if item["name"] == "session")
    session_switch = next(child for child in session["children"] if child["name"] == "switch")
    assert session_switch["arguments"][0]["provider"] == "sessions"


@pytest.mark.asyncio
async def test_slash_argument_choices_return_dynamic_candidates(tmp_path):
    import personal_agent.plugins.builtin.tools.builtin.file_read  # noqa: F401

    runtime = Runtime(tmp_path)

    tool_choices = await slash_argument_choices(
        runtime,
        "tools",
        command="tools",
        args=("show",),
        query="rea",
    )
    assert any(choice["value"] == "read" for choice in tool_choices)
    assert all("append_space" in choice for choice in tool_choices)

    session_choices = await slash_argument_choices(
        runtime,
        "sessions",
        command="session",
        args=("switch",),
        query="wor",
    )
    assert session_choices == [{
        "value": "work",
        "label": "work",
        "description": "8 messages",
        "append_space": False,
    }]

    assert await slash_argument_choices(runtime, "missing", command="tools") == []


@pytest.mark.asyncio
async def test_shared_command_stop_reports_delegate_agent_count(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.stop_delegate_agents",
        lambda: 2,
    )

    result = await handle_slash_command(runtime, "/stop")

    assert result.response == "已停止。已请求停止 2 个子 agent。"
    assert runtime.agent._interrupt_requested


@pytest.mark.asyncio
async def test_shared_command_uses_exact_command_names(tmp_path):
    runtime = Runtime(tmp_path)

    result = await handle_slash_command(runtime, "/newsletter")

    assert not result.handled
    assert not runtime.reset_called


@pytest.mark.asyncio
async def test_shared_command_lists_shows_and_clears_agent_runs(tmp_path):
    from personal_agent.models.messages import NormalizedResponse
    from personal_agent.plugins.builtin.tools.builtin.delegate import (
        _delegate_task,
        reset_delegate,
        setup_delegate,
    )

    async def call_fn(messages, system_prompt, tools, max_tokens):
        return NormalizedResponse(text="agent result", usage={"input_tokens": 1, "output_tokens": 2})

    reset_delegate()
    setup_delegate(call_fn, tools=[], max_tokens=100)
    await _delegate_task("inspect", role="reviewer")
    runtime = Runtime(tmp_path)

    listed = await handle_slash_command(runtime, "/agents list")
    run_id = listed.response.splitlines()[1].split()[1]
    shown = await handle_slash_command(runtime, f"/agents show {run_id}")
    cleared = await handle_slash_command(runtime, "/agents clear")
    empty = await handle_slash_command(runtime, "/agent-runs")

    assert "子 agent 运行记录" in listed.response
    assert "reviewer" in listed.response
    assert "agent result" in shown.response
    assert "已清理 1 条" in cleared.response
    assert "暂无子 agent" in empty.response
    reset_delegate()
