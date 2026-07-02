"""Shared slash command service for Gateway and CLI runtimes."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Protocol


@dataclass
class CommandResult:
    handled: bool
    response: str | None = None
    continue_text: str | None = None

    @classmethod
    def unhandled(cls) -> "CommandResult":
        return cls(handled=False)

    @classmethod
    def reply(cls, response: str) -> "CommandResult":
        return cls(handled=True, response=response)

    @classmethod
    def continue_with(cls, text: str) -> "CommandResult":
        return cls(handled=True, continue_text=text)


class CommandRuntime(Protocol):
    settings: object
    plugin_manager: object | None

    @property
    def session_key(self) -> str: ...

    @property
    def source(self): ...

    async def get_agent(self): ...

    async def reset_session(self) -> str | None: ...

    async def switch_session(self, name: str) -> str: ...

    async def list_sessions(self) -> str: ...

    async def current_session(self) -> str: ...

    async def rename_session(self, name: str) -> str: ...

    async def delete_session(self, name: str | None = None) -> str: ...

    async def load_history(self) -> list[dict]: ...

    async def export_session(self) -> tuple[int, str]: ...

    async def clear_agent(self) -> None: ...

    def plugin_command_kwargs(self, args: str) -> dict: ...


async def handle_slash_command(runtime: CommandRuntime, text: str) -> CommandResult:
    text = text.strip()
    parsed = _parse_command(text)
    if parsed is None:
        return CommandResult.unhandled()
    command_name, args = parsed

    if command_name == "new":
        response = await runtime.reset_session()
        await runtime.clear_agent()
        return CommandResult.reply(response or "会话已重置。开始新的对话吧。")

    if command_name == "session":
        return CommandResult.reply(await _session(runtime, args))

    if command_name == "usage":
        return CommandResult.reply(await _usage(runtime, current_user_message=text))

    if command_name == "export":
        count, path = await runtime.export_session()
        return CommandResult.reply(f"已导出 {count} 条对话 -> {path}")

    if command_name == "allow":
        parts = args.split()
        return CommandResult.reply(await _allow(runtime, parts[0] if parts else "write"))

    if command_name == "stop":
        return CommandResult.reply(await _stop(runtime))

    if command_name in {"agents", "agent-runs"}:
        return CommandResult.reply(await _agents(args))

    if command_name == "help":
        return CommandResult.reply(help_text(runtime))

    plugin_manager = runtime.plugin_manager
    plugin_command = None
    plugin_scope = "slash"
    if plugin_manager is not None:
        plugin_command, plugin_scope = _find_plugin_command(
            plugin_manager,
            command_name,
            _plugin_command_scopes(runtime),
        )
    if plugin_command is not None:
        value = await plugin_manager.execute_command(
            plugin_command.name,
            scope=plugin_scope,
            **runtime.plugin_command_kwargs(args),
        )
        return CommandResult.reply(value or "")

    skill_text = await _prepare_skill(runtime, text)
    if skill_text is not None:
        return CommandResult.continue_with(skill_text)

    return CommandResult.unhandled()


def _parse_command(text: str) -> tuple[str, str] | None:
    if not text.startswith("/") or text == "/":
        return None
    body = text[1:].strip()
    if not body:
        return None
    command_name, _, args = body.partition(" ")
    return command_name, args.strip()


def _plugin_command_scopes(runtime: CommandRuntime) -> tuple[str, ...]:
    scopes = getattr(runtime, "plugin_command_scopes", ("slash",))
    if isinstance(scopes, str):
        scopes = (scopes,)
    cleaned = tuple(scope for scope in scopes if scope in {"slash", "cli", "both"})
    return cleaned or ("slash",)


def _find_plugin_command(plugin_manager, name: str, scopes: tuple[str, ...]):
    for scope in scopes:
        command = plugin_manager.get_command(name, scope=scope)
        if command is not None:
            return command, scope
    return None, scopes[0] if scopes else "slash"


async def _call_optional(runtime: CommandRuntime, name: str, *args, **kwargs):
    handler = getattr(runtime, name, None)
    if handler is None:
        return None
    value = handler(*args, **kwargs)
    if inspect.isawaitable(value):
        value = await value
    return value


async def _usage(runtime: CommandRuntime, *, current_user_message: str) -> str:
    custom = await _call_optional(runtime, "usage", current_user_message=current_user_message)
    if custom is not None:
        return str(custom)

    agent = await runtime.get_agent()
    history = await runtime.load_history()

    from personal_agent.context_budget import build_context_budget
    from personal_agent.context_budget import compose_context_text

    budget = await build_context_budget(
        messages=history,
        agent=agent,
        settings=runtime.settings,
        skills_summary=compose_context_text(
            getattr(agent, "_last_skill_summaries", ""),
            getattr(agent, "_last_skill_injection", ""),
        ),
        memory_injections=getattr(agent, "_last_memory_injections", ""),
        current_user_message=current_user_message,
    )
    threshold_line = ""
    if budget.compression_threshold:
        marker = "，已达到" if budget.over_compression_threshold else ""
        threshold_line = f"压缩阈值: {budget.compression_threshold:,} tokens{marker}\n"
    return (
        f"会话用量\n"
        f"API 调用: {agent.session_api_calls} 次\n"
        f"输入 tokens: {agent.session_prompt_tokens:,} (API 报告)\n"
        f"输出 tokens: {agent.session_completion_tokens:,} (API 报告)\n"
        f"\n上下文窗口 (估算)\n"
        f"已用: {budget.used:,} / {budget.context_limit:,} tokens ({budget.percent}%)\n"
        f"  system prompt: {budget.system_prompt:,}\n"
        f"  history messages: {budget.history_messages:,}\n"
        f"  tools schema: {budget.tools_schema:,}\n"
        f"  skills: {budget.skills:,}\n"
        f"  memory injections: {budget.memory_injections:,}\n"
        f"  MCP tools: {budget.mcp_tools:,}\n"
        f"剩余: {budget.remaining_context:,} tokens\n"
        f"{threshold_line}"
        f"\n本轮工具调用: {agent._tool_calls_this_turn} / {agent._max_tool_calls_per_turn}"
    )


async def _session(runtime: CommandRuntime, args: str) -> str:
    parts = args.split()
    if not parts or parts[0] == "current":
        return await runtime.current_session()
    action = parts[0]
    if action == "list":
        return await runtime.list_sessions()
    if action == "switch":
        if len(parts) < 2:
            return "用法: /session switch <name>"
        return await runtime.switch_session(parts[1])
    if action == "rename":
        if len(parts) < 2:
            return "用法: /session rename <name>"
        return await runtime.rename_session(parts[1])
    if action == "delete":
        target = parts[1] if len(parts) > 1 else None
        if target == "current":
            target = None
        return await runtime.delete_session(target)
    return await runtime.switch_session(action)


async def _allow(runtime: CommandRuntime, category: str) -> str:
    valid = {"write", "bash", "all"}
    if category not in valid:
        return f"用法: /allow [write|bash|all]，当前有效类别: {', '.join(sorted(valid))}"
    custom = await _call_optional(runtime, "allow_category", category)
    if custom is not None:
        return str(custom)
    agent = await runtime.get_agent()
    agent._destructive_allowed.add(category)
    return f"已授权 {category} 操作，本轮对话内有效。"


async def _agents(args: str) -> str:
    from personal_agent.plugins.builtin.tools.builtin.delegate import (
        clear_agent_runs,
        format_agent_run,
        format_agent_runs,
    )

    parts = args.split()
    if not parts or parts[0] == "list":
        limit = _parse_limit(parts[1] if len(parts) > 1 else "")
        return format_agent_runs(limit=limit)
    if parts[0] == "show":
        if len(parts) < 2:
            return "用法: /agents show <run_id>"
        return format_agent_run(parts[1])
    if parts[0] == "clear":
        count = clear_agent_runs()
        return f"已清理 {count} 条子 agent 运行记录。"
    return "用法: /agents [list [limit]|show <run_id>|clear]"


def _parse_limit(value: str) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None


async def _stop(runtime: CommandRuntime) -> str:
    custom = await _call_optional(runtime, "stop_agents")
    if custom is not None:
        return str(custom)
    agent = await runtime.get_agent()
    agent._interrupt_requested = True
    from personal_agent.tools.executor import interrupt_active_tool_executions
    from personal_agent.plugins.builtin.tools.builtin.delegate import stop_delegate_agents

    interrupt_active_tool_executions()
    stopped = stop_delegate_agents()
    if stopped:
        return f"已停止。已请求停止 {stopped} 个子 agent。"
    return "已停止。"


async def _prepare_skill(runtime: CommandRuntime, text: str) -> str | None:
    skill_name = text[1:].split()[0]
    if not skill_name:
        return None
    try:
        from personal_agent.skills.registry import skill_registry

        content = skill_registry.load(skill_name)
    except Exception:
        return None
    if not content:
        return None
    agent = await runtime.get_agent()
    agent._pending_skill_injection = f"[技能: {skill_name}]\n\n{content}"
    parts = text.split(None, 1)
    return parts[1] if len(parts) > 1 else "你好"


def help_text(runtime: CommandRuntime | None = None) -> str:
    lines = [
        "可用命令:",
        "/new - 重置当前会话",
        "/session [current|list|switch <name>|rename <name>|delete [name]] - 管理会话",
        "/usage - 查看当前会话上下文预算",
        "/allow [write|bash|all] - 授权危险操作",
        "/stop - 停止当前处理",
        "/export - 导出当前会话 JSONL",
        "/agents [list|show|clear] - 查看子 agent 运行记录",
        "/help - 显示此帮助",
        "/<skill-name> [message] - 加载技能后发送消息",
        "exit / quit / 空行 - 退出 CLI",
    ]
    plugin_lines = _plugin_command_help_lines(runtime)
    if plugin_lines:
        lines.extend(["", "插件命令:", *plugin_lines])
    return "\n".join(lines)


def _plugin_command_help_lines(runtime: CommandRuntime | None) -> list[str]:
    if runtime is None or runtime.plugin_manager is None:
        return []
    commands = getattr(runtime.plugin_manager, "commands", {})
    if not isinstance(commands, dict):
        return []
    scopes = set(_plugin_command_scopes(runtime))
    lines = []
    for name, entry in sorted(commands.items()):
        entry_scope = getattr(entry, "scope", "slash")
        if entry_scope != "both" and entry_scope not in scopes:
            continue
        description = getattr(entry, "description", "") or "插件命令"
        plugin_key = getattr(entry, "plugin_key", "")
        suffix = f" ({plugin_key})" if plugin_key else ""
        lines.append(f"/{name} - {description}{suffix}")
    return lines
