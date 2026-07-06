"""Shared slash command service for Gateway and CLI runtimes."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Any, Protocol


@dataclass
class CommandResult:
    handled: bool
    response: str | None = None
    continue_text: str | None = None
    kind: str = ""
    payload: dict[str, Any] | None = None

    @classmethod
    def unhandled(cls) -> "CommandResult":
        return cls(handled=False)

    @classmethod
    def reply(cls, response: str) -> "CommandResult":
        return cls(handled=True, response=response)

    @classmethod
    def structured(cls, response: str, *, kind: str, payload: dict[str, Any]) -> "CommandResult":
        return cls(handled=True, response=response, kind=kind, payload=payload)

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

    async def memory_report(self) -> dict: ...

    async def memory_entries(self, *, target: str = "all") -> list[dict]: ...

    async def memory_search(self, query: str, *, target: str = "all") -> list[dict]: ...

    async def memory_entry(self, identifier: str, *, target: str = "all") -> dict | None: ...

    async def memory_delete(self, identifier: str, *, target: str = "all") -> bool: ...

    async def activity_snapshot(self, *, limit: int = 20) -> dict: ...

    async def activity_detail(self, kind: str, id_: str) -> dict | None: ...

    async def activity_choices(self, provider: str, *, query: str = "", limit: int = 20) -> list[dict]: ...

    def plugin_command_kwargs(self, args: str) -> dict: ...


@dataclass(frozen=True)
class ModeChoice:
    slug: str
    label: str
    profile: str


MODE_CHOICES: tuple[ModeChoice, ...] = (
    ModeChoice("read-only", "Read Only", "guarded"),
    ModeChoice("ask-first", "Ask First", "standard"),
    ModeChoice("edit-freely", "Edit Freely", "trusted"),
    ModeChoice("full-auto", "Full Auto", "sovereign"),
)

_MODE_BY_SLUG = {choice.slug: choice for choice in MODE_CHOICES}
_MODE_BY_PROFILE = {choice.profile: choice for choice in MODE_CHOICES}
_MODE_USAGE = "/mode [Read Only|Ask First|Edit Freely|Full Auto]"
_MODE_ALIASES = {
    "readonly": "read-only",
    "read": "read-only",
    "guarded": "read-only",
    "askfirst": "ask-first",
    "ask": "ask-first",
    "standard": "ask-first",
    "normal": "ask-first",
    "editfreely": "edit-freely",
    "edit": "edit-freely",
    "edits": "edit-freely",
    "trusted": "edit-freely",
    "acceptedits": "edit-freely",
    "fullauto": "full-auto",
    "full": "full-auto",
    "auto": "full-auto",
    "sovereign": "full-auto",
}


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

    if command_name == "mode":
        return CommandResult.reply(await _mode(runtime, args))

    if command_name == "stop":
        return CommandResult.reply(await _stop(runtime))

    if command_name in {"agents", "agent-runs"}:
        return CommandResult.reply(await _agents(args))

    if command_name == "activity":
        payload, response = await _activity(runtime, args)
        return CommandResult.structured(response, kind="activity", payload=payload)

    if command_name == "memory":
        return CommandResult.reply(await _memory(runtime, args))

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
    valid = {"write", "bash", "background", "network", "destructive", "default", "all"}
    if category not in valid:
        return f"用法: /allow [write|bash|background|network|destructive|all]，当前有效类别: {', '.join(sorted(valid))}"
    custom = await _call_optional(runtime, "allow_category", category)
    if custom is not None:
        return str(custom)
    agent = await runtime.get_agent()
    agent._destructive_allowed.add(category)
    return f"已授权 {category} 操作，本轮对话内有效。"


def current_mode(agent) -> str:
    """Return the user-facing execution mode label for an agent."""
    return current_mode_from_policy(getattr(agent, "_execution_policy", None))


def current_mode_from_policy(policy) -> str:
    """Return the user-facing execution mode label for an execution policy."""
    choice = _choice_for_profile(str(getattr(policy, "mode", "") or ""))
    return choice.label


async def _mode(runtime: CommandRuntime, args: str) -> str:
    agent = await runtime.get_agent()
    requested = args.strip()
    if not requested:
        return f"当前模式: {current_mode(agent)}。用法: {_MODE_USAGE}"
    choice = _choice_for_input(requested)
    if choice is None:
        return f"用法: {_MODE_USAGE}"
    custom = await _call_optional(runtime, "set_mode", choice.slug)
    if custom is not None:
        return str(custom)
    from personal_agent.execution import resolve_execution_policy_for_mode

    agent._execution_policy = resolve_execution_policy_for_mode(runtime.settings, choice.profile)
    grants = getattr(agent, "_destructive_allowed", None)
    if grants is None:
        agent._destructive_allowed = set()
    else:
        grants.clear()
    return f"执行模式已切换: {choice.label}（{choice.profile}）。"


def _choice_for_input(value: str) -> ModeChoice | None:
    key = _mode_key(value)
    slug = _MODE_ALIASES.get(key)
    if slug:
        return _MODE_BY_SLUG[slug]
    return None


def _choice_for_profile(profile: str) -> ModeChoice:
    return _MODE_BY_PROFILE.get(profile, _MODE_BY_PROFILE["standard"])


def _mode_key(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


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


async def _activity(runtime: CommandRuntime, args: str) -> tuple[dict[str, Any], str]:
    parts = args.split()
    scope = _activity_scope(parts[0]) if parts else "all"
    if scope == "":
        payload = {"error": "usage", "usage": "/activity [agents|processes|gateway] [id]"}
        return payload, "用法: /activity [agents|processes|gateway] [id]"

    if len(parts) >= 2:
        detail = await _activity_detail(runtime, scope, parts[1])
        if detail is None:
            payload = {
                "scope": scope,
                "id": parts[1],
                "not_found": True,
            }
            return payload, f"未找到 activity: {scope} {parts[1]}"
        return detail, _format_activity_detail(detail)

    snapshot = await _activity_snapshot(runtime)
    payload = snapshot if scope == "all" else _activity_scope_payload(snapshot, scope)
    return payload, _format_activity_payload(payload, scope=scope)


async def _activity_snapshot(runtime: CommandRuntime) -> dict[str, Any]:
    custom = await _call_optional(runtime, "activity_snapshot", limit=20)
    if custom is not None:
        return dict(custom)
    from personal_agent.activity import activity_snapshot

    return activity_snapshot()


async def _activity_detail(runtime: CommandRuntime, scope: str, id_: str) -> dict[str, Any] | None:
    kind = {
        "agents": "sub_agent",
        "processes": "background_process",
        "gateway": "gateway_agent",
    }[scope]
    custom = await _call_optional(runtime, "activity_detail", kind, id_)
    if custom is not None:
        return custom
    from personal_agent.activity import activity_detail

    return activity_detail(kind, id_)


def _activity_scope(raw: str) -> str:
    value = raw.strip().lower()
    aliases = {
        "all": "all",
        "summary": "all",
        "list": "all",
        "agents": "agents",
        "agent": "agents",
        "sub_agents": "agents",
        "sub-agent": "agents",
        "sub-agents": "agents",
        "processes": "processes",
        "process": "processes",
        "background": "processes",
        "background_processes": "processes",
        "gateway": "gateway",
        "gateways": "gateway",
        "gateway_agents": "gateway",
    }
    return aliases.get(value, "")


def _activity_scope_payload(snapshot: dict[str, Any], scope: str) -> dict[str, Any]:
    key = {
        "agents": "sub_agents",
        "processes": "background_processes",
        "gateway": "gateway_agents",
    }[scope]
    return {
        "scope": scope,
        "summary": dict(snapshot.get("summary") or {}),
        key: dict(snapshot.get(key) or {}),
    }


def _format_activity_payload(payload: dict[str, Any], *, scope: str) -> str:
    if scope == "agents":
        return _format_activity_agents(payload.get("sub_agents") or {})
    if scope == "processes":
        return _format_activity_processes(payload.get("background_processes") or {})
    if scope == "gateway":
        return _format_activity_gateway(payload.get("gateway_agents") or {})
    return "\n".join([
        _format_activity_summary(payload),
        "",
        _format_activity_agents(payload.get("sub_agents") or {}),
        "",
        _format_activity_processes(payload.get("background_processes") or {}),
        "",
        _format_activity_gateway(payload.get("gateway_agents") or {}),
    ]).strip()


def _format_activity_summary(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    counts = summary.get("counts") or {}
    sub_agents = counts.get("sub_agents") or {}
    processes = counts.get("background_processes") or {}
    gateway = counts.get("gateway_agents") or {}
    attention = "是" if summary.get("attention_required") else "否"
    return "\n".join([
        "运行活动",
        f"活跃任务: {int(summary.get('active_total') or 0)}",
        f"需要注意: {attention}",
        f"最长运行: {float(summary.get('longest_running_seconds') or 0.0):.1f}s",
        (
            "子 agent: "
            f"active={int(sub_agents.get('active') or 0)} "
            f"recent={int(sub_agents.get('recent') or 0)} "
            f"failed_recent={int(sub_agents.get('failed_recent') or 0)}"
        ),
        (
            "后台任务: "
            f"running={int(processes.get('running') or 0)} "
            f"done={int(processes.get('done') or 0)} "
            f"killed={int(processes.get('killed') or 0)}"
        ),
        f"Gateway agent: running={int(gateway.get('running') or 0)}",
    ])


def _format_activity_agents(payload: dict[str, Any]) -> str:
    active = payload.get("active_runs") or []
    recent = payload.get("recent_runs") or []
    lines = [f"子 agent: active={len(active)} recent={len(recent)}"]
    for item in active:
        lines.append(_format_activity_item(item))
    for item in recent:
        lines.append(_format_activity_item(item))
    if len(lines) == 1:
        lines.append("暂无子 agent 活动。")
    return "\n".join(lines)


def _format_activity_processes(payload: dict[str, Any]) -> str:
    items = payload.get("items") or []
    counts = payload.get("counts") or {}
    lines = [
        (
            "后台任务: "
            f"total={int(counts.get('total') or 0)} "
            f"running={int(counts.get('running') or 0)} "
            f"done={int(counts.get('done') or 0)} "
            f"killed={int(counts.get('killed') or 0)}"
        )
    ]
    for item in items:
        lines.append(_format_activity_item(item))
    if len(lines) == 1:
        lines.append("暂无后台任务。")
    return "\n".join(lines)


def _format_activity_gateway(payload: dict[str, Any]) -> str:
    items = payload.get("running_agent_runs") or []
    counts = payload.get("counts") or {}
    lines = [
        (
            "Gateway agent: "
            f"running={int(counts.get('running') or 0)} "
            f"stop_requested={int(counts.get('stop_requested') or 0)}"
        )
    ]
    for item in items:
        lines.append(_format_activity_item(item))
    if len(lines) == 1:
        lines.append("暂无 Gateway agent 活动。")
    return "\n".join(lines)


def _format_activity_item(item: dict[str, Any]) -> str:
    item_id = item.get("id") or item.get("run_id") or item.get("session_key") or "-"
    status = item.get("status") or "-"
    duration = float(item.get("duration_seconds") or 0.0)
    title = item.get("task") or item.get("command") or item.get("platform") or ""
    suffix = f" - {_short_text(str(title), 90)}" if title else ""
    return f"- {item_id} [{status}] {duration:.1f}s{suffix}"


def _format_activity_detail(payload: dict[str, Any]) -> str:
    item = payload.get("run") or payload.get("process") or payload.get("gateway_run") or {}
    kind = payload.get("kind") or item.get("kind") or "activity"
    item_id = payload.get("id") or item.get("id") or "-"
    lines = [
        f"Activity detail: {kind} {item_id}",
        f"status: {item.get('status') or '-'}",
        f"duration: {float(item.get('duration_seconds') or 0.0):.1f}s",
        f"started_at: {item.get('started_at') or '-'}",
    ]
    if item.get("finished_at"):
        lines.append(f"finished_at: {item.get('finished_at')}")
    if item.get("error"):
        lines.append(f"error: {item.get('error')}")
    title = item.get("task") or item.get("command") or item.get("platform")
    if title:
        lines.append(str(title))
    return "\n".join(lines)


async def _memory(runtime: CommandRuntime, args: str) -> str:
    parts = args.split()
    action = parts[0] if parts else "list"
    rest, target = _split_target_args(parts[1:])

    if action == "doctor":
        custom = await _call_optional(runtime, "format_memory_doctor")
        if custom is not None:
            return str(custom)
        return _format_memory_doctor(await runtime.memory_report())

    if action == "list":
        custom = await _call_optional(runtime, "format_memory_entries", target=target)
        if custom is not None:
            return str(custom)
        return _format_memory_entries(await runtime.memory_entries(target=target))

    if action == "search":
        query = " ".join(token for token in rest if not token.startswith("--target=")).strip()
        if not query:
            return "用法: /memory search <query> [--target=all|memory|user|external]"
        custom = await _call_optional(runtime, "format_memory_search", query, target=target)
        if custom is not None:
            return str(custom)
        return _format_memory_entries(
            await runtime.memory_search(query, target=target),
            title="记忆搜索结果",
        )

    if action == "show":
        if not rest:
            return "用法: /memory show <id> [--target=all|memory|user|external]"
        identifier = rest[0]
        entry = await runtime.memory_entry(identifier, target=target)
        if entry is None:
            return f"未找到记忆: {identifier}"
        custom = await _call_optional(runtime, "format_memory_entry", entry)
        if custom is not None:
            return str(custom)
        return _format_memory_entry(entry)

    if action == "delete":
        if not rest:
            return "用法: /memory delete <id> [--target=all|memory|user|external]"
        identifier = rest[0]
        deleted = await runtime.memory_delete(identifier, target=target)
        if not deleted:
            return f"未找到或无法删除记忆: {identifier}"
        return f"已删除记忆: {identifier}"

    return "用法: /memory [list|search <query>|show <id>|delete <id>|doctor]"


def _split_target_args(tokens: list[str]) -> tuple[list[str], str]:
    target = "all"
    rest: list[str] = []
    skip_next = False
    for index, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if token.startswith("--target="):
            target = token.split("=", 1)[1] or target
        elif token in {"--target", "-t"} and index + 1 < len(tokens):
            target = tokens[index + 1]
            skip_next = True
        else:
            rest.append(token)
    return rest, target


def _format_memory_doctor(report: dict) -> str:
    providers = report.get("providers") or {}
    builtin = providers.get("builtin") or {}
    external = providers.get("external") or {}
    return "\n".join([
        "Memory 诊断",
        f"builtin: {builtin.get('provider') or report.get('builtin_provider') or '-'} available={bool(builtin.get('available', report.get('builtin_available', False)))} entries={builtin.get('entries', 0)}",
        f"external: {external.get('provider') or report.get('external_provider') or '-'} available={bool(external.get('available', report.get('external_available', False)))} entries={external.get('entries', 0)}",
        f"last_errors: {report.get('last_errors') or {}}",
    ])


def _format_memory_entries(entries: list[dict], *, title: str = "记忆列表") -> str:
    if not entries:
        return f"{title}: 无"
    lines = [f"{title}: {len(entries)} 条"]
    for entry in entries:
        label = entry.get("id") or entry.get("index") or "-"
        provider = entry.get("provider") or "-"
        target = entry.get("target") or "-"
        text = _short_text(str(entry.get("text", "")), 120)
        lines.append(f"- {label} [{provider}/{target}] {text}")
    return "\n".join(lines)


def _format_memory_entry(entry: dict) -> str:
    return "\n".join([
        f"记忆: {entry.get('id') or entry.get('index') or '-'}",
        f"provider: {entry.get('provider') or '-'}",
        f"target: {entry.get('target') or '-'}",
        f"created_at: {entry.get('created_at') or '-'}",
        f"path: {entry.get('path') or '-'}",
        "",
        str(entry.get("text", "")),
    ])


def _short_text(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


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
        "/mode [Read Only|Ask First|Edit Freely|Full Auto] - 切换执行模式",
        "/stop - 停止当前处理",
        "/export - 导出当前会话 JSONL",
        "/agents [list|show|clear] - 查看子 agent 运行记录",
        "/activity [agents|processes|gateway] [id] - 查看子 agent、后台任务和 Gateway agent 活动",
        "/memory [list|search|show|delete|doctor] - 查看和管理记忆",
        "/help - 显示此帮助",
        "/<skill-name> [message] - 加载技能后发送消息",
        "exit / quit / 空行 - 退出 CLI",
    ]
    if runtime is not None and "cli" in _plugin_command_scopes(runtime):
        lines.insert(-1, '""" - 进入多行输入，再输入 """ 提交，/cancel 取消')
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
