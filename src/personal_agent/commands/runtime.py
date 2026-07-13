"""Shared slash command service for Gateway and CLI runtimes."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import json
from typing import Any, Protocol

from personal_agent.commands.registry import (
    command_specs_as_dict,
    find_command_spec,
    format_command_detail,
    format_commands,
    suggest_command_names,
)
from personal_agent.permissions import (
    add_temporary_grant,
    add_turn_grants,
    format_expiry,
    remove_temporary_grant,
    temporary_grant_ttl_seconds,
    temporary_grants_snapshot,
)


@dataclass
class CommandResult:
    handled: bool
    response: str | None = None
    continue_text: str | None = None
    payload: dict | None = None
    kind: str = "text"
    error: str | None = None
    suggestions: list[str] | None = None

    @classmethod
    def unhandled(cls) -> "CommandResult":
        return cls(handled=False)

    @classmethod
    def reply(
        cls,
        response: str,
        *,
        payload: dict | None = None,
        kind: str = "text",
    ) -> "CommandResult":
        return cls(handled=True, response=response, payload=payload, kind=kind)

    @classmethod
    def error_reply(
        cls,
        response: str,
        *,
        payload: dict | None = None,
        suggestions: list[str] | None = None,
        kind: str = "error",
    ) -> "CommandResult":
        return cls(
            handled=True,
            response=response,
            payload=payload,
            kind=kind,
            error=response,
            suggestions=list(suggestions or []),
        )

    @classmethod
    def structured(cls, response: str, *, kind: str, payload: dict[str, Any]) -> "CommandResult":
        return cls(handled=True, response=response, kind=kind, payload=payload)

    @classmethod
    def continue_with(cls, text: str) -> "CommandResult":
        return cls(handled=True, continue_text=text, kind="continue")


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

    async def tool_runs_recent(self, *, limit: int = 10, all_sessions: bool = False) -> dict: ...

    async def tool_run_detail(self, run_id: int) -> dict | None: ...

    async def tool_runs_summary(self, *, limit: int = 50, all_sessions: bool = False) -> dict: ...

    async def activity_snapshot(self, *, limit: int = 20) -> dict: ...

    async def activity_detail(self, kind: str, id_: str) -> dict | None: ...

    async def activity_choices(self, provider: str, *, query: str = "", limit: int = 20) -> list[dict]: ...

    async def is_session_running(self) -> bool: ...

    async def add_steer(self, text: str) -> str: ...

    async def steer_snapshot(self) -> dict: ...

    def plugin_command_kwargs(self, args: str) -> dict: ...


@dataclass(frozen=True)
class ModeChoice:
    slug: str
    label: str
    profile: str


MODE_CHOICES: tuple[ModeChoice, ...] = (
    ModeChoice("read-only", "Read Only", "guarded"),
    ModeChoice("ask-first", "Ask First", "standard"),
    ModeChoice("local-auto", "Local Auto", "trusted"),
    ModeChoice("full-auto", "Full Auto", "sovereign"),
)

_MODE_BY_SLUG = {choice.slug: choice for choice in MODE_CHOICES}
_MODE_BY_PROFILE = {choice.profile: choice for choice in MODE_CHOICES}
_MODE_USAGE = "/mode [Read Only|Ask First|Local Auto|Full Auto]"
_MODE_ALIASES = {
    "readonly": "read-only",
    "read": "read-only",
    "guarded": "read-only",
    "askfirst": "ask-first",
    "ask": "ask-first",
    "standard": "ask-first",
    "normal": "ask-first",
    "editfreely": "local-auto",
    "edit": "local-auto",
    "edits": "local-auto",
    "trusted": "local-auto",
    "acceptedits": "local-auto",
    "autoedit": "local-auto",
    "localauto": "local-auto",
    "fullauto": "full-auto",
    "full": "full-auto",
    "auto": "full-auto",
    "sovereign": "full-auto",
}

_ACTIVITY_SCOPE_CHOICES = (
    ("agents", "agents", "Sub-agent runs"),
    ("processes", "processes", "Background processes"),
    ("gateway", "gateway", "Gateway agent runs"),
)


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

    if command_name == "deny":
        parts = args.split()
        return CommandResult.reply(await _deny(runtime, parts[0] if parts else "all"))

    if command_name == "mode":
        text, payload = await _mode(runtime, args)
        if payload.get("error"):
            return CommandResult.error_reply(text, payload=payload, suggestions=payload.get("suggestions"))
        return CommandResult.reply(text, payload=payload, kind="mode")

    if command_name == "permissions":
        text, payload = await _permissions(runtime, args)
        if payload.get("error"):
            return CommandResult.error_reply(text, payload=payload, suggestions=payload.get("suggestions"))
        return CommandResult.reply(text, payload=payload, kind="permissions")

    if command_name == "stop":
        return CommandResult.reply(await _stop(runtime))

    if command_name == "steer":
        return CommandResult.reply(await _steer(runtime, args))

    if command_name in {"agents", "agent-runs"}:
        return CommandResult.reply(await _agents(args))

    if command_name == "activity":
        payload, response = await _activity(runtime, args)
        if payload.get("error"):
            return CommandResult.error_reply(response, payload=payload, kind="activity_error")
        return CommandResult.structured(response, kind="activity", payload=payload)

    if command_name == "memory":
        return CommandResult.reply(await _memory(runtime, args))

    if command_name == "tools":
        text, payload = await _tools(runtime, args)
        if payload.get("error"):
            return CommandResult.error_reply(text, payload=payload, suggestions=payload.get("suggestions"))
        return CommandResult.reply(text, payload=payload, kind="tools")

    if command_name == "tool-runs":
        text, payload = await _tool_runs(runtime, args)
        if payload.get("error"):
            return CommandResult.error_reply(text, payload=payload, suggestions=payload.get("suggestions"))
        return CommandResult.reply(text, payload=payload, kind="tool_runs")

    if command_name == "protocol":
        text, payload = _protocol(args)
        return CommandResult.reply(text, payload=payload, kind="protocol")

    if command_name == "commands":
        text, payload = _commands(runtime, args)
        if payload.get("error"):
            return CommandResult.error_reply(text, payload=payload, suggestions=payload.get("suggestions"))
        return CommandResult.reply(text, payload=payload, kind="commands")

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

    suggestions = suggest_command_names(runtime, command_name)
    if suggestions:
        response = f"未找到命令: /{command_name}\n你是不是想用: {', '.join(suggestions)}"
        return CommandResult.error_reply(
            response,
            payload={
                "command": command_name,
                "suggestions": suggestions,
            },
            suggestions=suggestions,
            kind="command_error",
        )

    return CommandResult.unhandled()


def slash_command_metadata(runtime: CommandRuntime | None = None) -> list[dict[str, Any]]:
    """Return frontend-consumable slash command registry metadata."""
    payload = command_specs_as_dict(runtime)
    entries = list(payload.get("commands") or [])
    entries.extend(list(payload.get("plugin_commands") or []))
    return entries


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


def _normalize_choice_items(items: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    choices = []
    for item in items:
        value = str(item.get("value") or "")
        if not value:
            continue
        choices.append({
            "value": value,
            "label": str(item.get("label") or value),
            "description": str(item.get("description") or ""),
            "append_space": bool(item.get("append_space", False)),
        })
        if len(choices) >= limit:
            break
    return choices


async def slash_argument_choices(
    runtime: CommandRuntime,
    provider: str,
    *,
    command: str,
    args: tuple[str, ...] = (),
    query: str = "",
    limit: int = 20,
) -> list[dict]:
    """Return frontend-facing slash argument candidates for dynamic providers."""
    normalized_provider = str(provider or "").strip()
    if normalized_provider == "tools":
        return _tool_argument_choices(runtime, query=query, limit=limit)
    if normalized_provider == "sessions":
        return await _session_argument_choices(runtime, query=query, limit=limit)
    if normalized_provider in {"activity_agents", "activity_processes", "activity_gateway"}:
        custom = await _call_optional(
            runtime,
            "activity_choices",
            normalized_provider,
            query=query,
            limit=limit,
        )
        if custom is not None:
            return _normalize_choice_items(custom, limit=limit)
        from personal_agent.activity import activity_choices

        return _normalize_choice_items(
            activity_choices(normalized_provider, query=query, limit=limit),
            limit=limit,
        )
    return []


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
    recent_tool_calls = len(getattr(agent, "_last_tool_results", []) or [])
    max_tool_calls = int(getattr(agent, "_max_tool_calls_per_turn", 0) or 0)
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
        f"\n最近一轮工具执行: {recent_tool_calls} 次\n"
        f"单轮工具上限: {max_tool_calls} 次"
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
    agent = _runtime_cached_agent(runtime)
    policy = getattr(agent, "_execution_policy", None) if agent is not None else getattr(runtime.settings, "execution_policy", None)
    denied = _allow_denied_categories(policy, category)
    if category != "all" and denied:
        return _allow_denied_message(policy, category)
    if category == "all" and len(denied) == len(_ALLOW_ALL_CATEGORIES):
        return _allow_denied_message(policy, category)

    warning = _allow_warning(denied)
    if agent is None:
        agent = await runtime.get_agent()
    ttl = temporary_grant_ttl_seconds(runtime.settings)
    if category == "all":
        expires_at = 0.0
        for item in _ALLOW_ALL_CATEGORIES:
            if item not in denied:
                expires_at = add_temporary_grant(agent, item, ttl_seconds=ttl)
                add_turn_grants(agent, item)
    else:
        expires_at = add_temporary_grant(agent, category, ttl_seconds=ttl)
        add_turn_grants(agent, category)
    response = f"已授权 {category}，{_format_ttl(ttl)}内有效，至 {format_expiry(expires_at)} 失效。"
    return f"{response}\n{warning}" if warning else response


_ALLOW_ALL_CATEGORIES = ("write", "bash", "background", "network", "destructive", "default")


async def _deny(runtime: CommandRuntime, category: str) -> str:
    valid = {"write", "bash", "background", "network", "destructive", "default", "all"}
    if category not in valid:
        return f"用法: /deny [write|bash|background|network|destructive|all]，当前有效类别: {', '.join(sorted(valid))}"
    agent = _runtime_cached_agent(runtime)
    if agent is None:
        return "当前没有可撤销的临时授权。"
    if category == "all":
        changed = remove_temporary_grant(agent, "all")
        return "已撤销全部临时授权。" if changed else "当前没有可撤销的临时授权。"
    changed = remove_temporary_grant(agent, category)
    return f"已撤销 {category} 临时授权。" if changed else f"当前没有 {category} 临时授权。"


def _format_ttl(seconds: int) -> str:
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours}小时"
    minutes = max(1, seconds // 60)
    return f"{minutes}分钟"


def _runtime_cached_agent(runtime: CommandRuntime):
    agent = getattr(runtime, "agent", None)
    if agent is not None:
        return agent
    service = getattr(runtime, "conversation_service", None)
    if service is not None and hasattr(service, "get_cached_agent"):
        try:
            return service.get_cached_agent(runtime.session_key)
        except Exception:
            return None
    return None


def _allow_denied_categories(policy, category: str) -> list[str]:
    categories = _ALLOW_ALL_CATEGORIES if category == "all" else (category,)
    if policy is None:
        return []
    denied: list[str] = []
    for item in categories:
        try:
            decision = str(policy.permission_for(item))
        except Exception:
            decision = ""
        if decision == "deny":
            denied.append(item)
    return denied


def _allow_denied_message(policy, category: str) -> str:
    mode = current_mode_from_policy(policy)
    if category == "all":
        return f"当前模式 {mode} 禁止这些权限类别，/allow all 不能覆盖。请切换模式或修改 config.yaml 的 execution.policy。"
    return f"当前模式 {mode} 禁止 {category}，/allow 不能覆盖。请切换模式或修改 config.yaml 的 execution.policy。"


def _allow_warning(denied: list[str]) -> str:
    if not denied:
        return ""
    return f"注意：当前模式仍禁止 {', '.join(denied)}，/allow 不能覆盖。"


def current_mode(agent) -> str:
    """Return the user-facing execution mode label for an agent."""
    security_context = getattr(agent, "_security_context", None)
    if security_context is not None:
        from personal_agent.security.modes import mode_preset

        return mode_preset(getattr(security_context, "mode_id", "ask-first")).label
    return current_mode_from_policy(getattr(agent, "_execution_policy", None))


def current_mode_from_policy(policy) -> str:
    """Return the user-facing execution mode label for an execution policy."""
    choice = _choice_for_profile(str(getattr(policy, "mode", "") or ""))
    return choice.label


async def _mode(runtime: CommandRuntime, args: str) -> tuple[str, dict]:
    agent = await runtime.get_agent()
    requested = args.strip()
    current = _mode_payload(current_mode(agent), getattr(getattr(agent, "_execution_policy", None), "mode", "") or "standard")
    if not requested or requested == "show":
        return f"当前模式: {current['current']['label']}。用法: {_MODE_USAGE}", {
            "action": "show",
            **current,
        }
    if requested == "list":
        return "执行模式:\n" + "\n".join(f"- {choice.label} ({choice.profile})" for choice in MODE_CHOICES), {
            "action": "list",
            **current,
        }
    if requested.startswith("set "):
        requested = requested.split(None, 1)[1].strip()
    choice = _choice_for_input(requested)
    if choice is None:
        suggestions = _mode_suggestions(requested)
        text = f"用法: {_MODE_USAGE}"
        if suggestions:
            text += "\n你是不是想用: " + ", ".join(f"/mode set {item}" for item in suggestions)
        return text, {
            "action": "set",
            "requested": requested,
            "error": "unknown_mode",
            "suggestions": [f"/mode set {item}" for item in suggestions],
            **current,
        }
    custom = await _call_optional(runtime, "set_mode", choice.slug)
    if custom is not None:
        return str(custom), {
            "action": "set",
            "selected": _choice_payload(choice),
            "custom": True,
            **_mode_payload(choice.label, choice.profile),
        }
    from personal_agent.execution import resolve_execution_policy_for_mode

    agent._execution_policy = resolve_execution_policy_for_mode(runtime.settings, choice.profile)
    for attr in ("_destructive_allowed", "_turn_grants"):
        grants = getattr(agent, attr, None)
        if grants is None:
            setattr(agent, attr, set())
        else:
            grants.clear()
    return f"执行模式已切换: {choice.label}（{choice.profile}）。", {
        "action": "set",
        "selected": _choice_payload(choice),
        "cleared_grants": True,
        **_mode_payload(choice.label, choice.profile),
    }


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


def _mode_payload(label: str, profile: str) -> dict:
    return {
        "current": {
            "label": label,
            "profile": profile,
            "slug": _choice_for_profile(profile).slug,
        },
        "modes": [_choice_payload(choice) for choice in MODE_CHOICES],
    }


def _choice_payload(choice: ModeChoice) -> dict:
    return {
        "slug": choice.slug,
        "label": choice.label,
        "profile": choice.profile,
    }


def _mode_suggestions(value: str) -> list[str]:
    from difflib import get_close_matches

    candidates = [choice.label for choice in MODE_CHOICES]
    aliases = sorted(_MODE_ALIASES)
    key = _mode_key(value)
    alias_matches = get_close_matches(key, aliases, n=2, cutoff=0.72)
    labels = [choice.label for alias in alias_matches for choice in [_MODE_BY_SLUG[_MODE_ALIASES[alias]]]]
    label_matches = get_close_matches(str(value or ""), candidates, n=2, cutoff=0.55)
    result: list[str] = []
    for item in [*labels, *label_matches]:
        if item not in result:
            result.append(item)
    return result[:3]


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
    title = item.get("task_preview") or item.get("task") or item.get("command_preview")
    title = title or item.get("command") or item.get("platform") or ""
    suffix = f" - {_short_text(str(title), 90)}" if title else ""
    extras = []
    if int(item.get("pending_steers") or 0):
        extras.append(f"steer={int(item.get('pending_steers') or 0)}")
    extra = f" ({', '.join(extras)})" if extras else ""
    return f"- {item_id} [{status}] {duration:.1f}s{extra}{suffix}"


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


def _commands(runtime: CommandRuntime, args: str) -> tuple[str, dict]:
    value = args.strip()
    if value == "json":
        payload = command_specs_as_dict(runtime)
        return json.dumps(payload, indent=2, ensure_ascii=False), payload
    if value:
        spec = find_command_spec(value)
        if spec is None:
            suggestions = suggest_command_names(runtime, value)
            text = f"未找到命令: {value}"
            if suggestions:
                text += "\n你是不是想用: " + ", ".join(suggestions)
            return text, {
                "query": value,
                "error": "unknown_command",
                "suggestions": suggestions,
            }
        return format_command_detail(value, runtime), {
            "command": spec.as_dict(),
        }
    payload = command_specs_as_dict(runtime)
    return format_commands(runtime), payload


async def _tools(runtime: CommandRuntime, args: str) -> tuple[str, dict]:
    parts = args.split()
    action = parts[0] if parts else "list"
    if action == "list":
        return _tools_list(runtime)
    if action == "show":
        if len(parts) < 2:
            return "用法: /tools show <name>", {
                "action": "show",
                "error": "missing_tool_name",
            }
        return _tools_show(runtime, parts[1])
    if action not in {"list", "show"}:
        suggestions = _tool_command_suggestions(action)
        if suggestions:
            return (
                f"未知 tools 子命令: {action}\n你是不是想用: "
                + ", ".join(f"/tools {item}" for item in suggestions),
                {
                    "action": action,
                    "error": "unknown_tools_action",
                    "suggestions": [f"/tools {item}" for item in suggestions],
                },
            )
    # Convenience: /tools bash behaves like /tools show bash.
    return _tools_show(runtime, action)


def _tools_list(runtime: CommandRuntime) -> tuple[str, dict]:
    from personal_agent.tools.registry import tool_registry

    summary = tool_registry.catalog_summary(_enabled_toolsets(runtime))
    lines = [
        "工具总览",
        f"total: {summary['total']} available: {summary['available']} unavailable: {summary['unavailable']}",
        f"by toolset: {_format_counts(summary.get('by_toolset'))}",
        f"by permission: {_format_counts(summary.get('by_permission'))}",
        f"by risk: {_format_counts(summary.get('by_risk'))}",
    ]
    high_risk = summary.get("high_risk") or []
    if high_risk:
        lines.append("high risk: " + ", ".join(str(item) for item in high_risk[:20]))
    lines.append("用法: /tools show <name>")
    return "\n".join(lines), {
        "action": "list",
        "summary": summary,
    }


def _tools_show(runtime: CommandRuntime, name: str) -> tuple[str, dict]:
    from personal_agent.tools.registry import tool_registry

    target = str(name or "").strip()
    items = tool_registry.catalog(_enabled_toolsets(runtime))
    for item in items:
        if item.get("name") != target:
            continue
        return "\n".join([
            f"工具: {item.get('name')}",
            f"description: {item.get('description') or '-'}",
            f"toolset: {item.get('toolset') or '-'}",
            f"permission: {item.get('permission_category') or '-'}",
            f"risk: {item.get('risk_level') or '-'}",
            f"available: {_yes(bool(item.get('available')))}",
            f"destructive: {_yes(bool(item.get('is_destructive')))}",
            f"parallel safe: {_yes(bool(item.get('is_parallel_safe')))}",
            f"usage: {item.get('usage_hint') or '-'}",
            f"inputs: {', '.join(item.get('input_properties') or []) or '-'}",
        ]), {
            "action": "show",
            "tool": item,
        }
    suggestions = _tool_name_suggestions(target, items)
    text = f"未找到工具: {target}"
    if suggestions:
        text += "\n你是不是想用: " + ", ".join(f"/tools show {item}" for item in suggestions)
    return text, {
        "action": "show",
        "query": target,
        "error": "unknown_tool",
        "suggestions": [f"/tools show {item}" for item in suggestions],
    }


async def _tool_runs(runtime: CommandRuntime, args: str) -> tuple[str, dict]:
    parts = args.split()
    action = parts[0] if parts else "recent"
    rest = parts[1:] if parts else []
    if action == "list":
        action = "recent"
    if action in {"recent", "summary"}:
        options = _parse_tool_run_options(rest)
        if options.get("error"):
            return str(options["message"]), {
                "action": action,
                "error": options["error"],
            }
        limit = int(options["limit"])
        all_sessions = bool(options["all_sessions"])
        if action == "summary":
            payload = await runtime.tool_runs_summary(
                limit=limit,
                all_sessions=all_sessions,
            )
            payload = {"action": "summary", **payload}
            return _format_tool_runs_summary_text(payload), payload
        payload = await runtime.tool_runs_recent(
            limit=limit,
            all_sessions=all_sessions,
        )
        payload = {"action": "recent", **payload}
        return _format_tool_runs_recent_text(payload), payload
    if action == "show":
        if not rest:
            return "用法: /tool-runs show <id>", {
                "action": "show",
                "error": "missing_tool_run_id",
            }
        run_id = _parse_int(rest[0])
        if run_id is None:
            return f"无效 tool run id: {rest[0]}", {
                "action": "show",
                "error": "invalid_tool_run_id",
                "query": rest[0],
            }
        item = await runtime.tool_run_detail(run_id)
        if item is None:
            return f"未找到 tool run: {run_id}", {
                "action": "show",
                "error": "unknown_tool_run",
                "id": run_id,
            }
        payload = {
            "action": "show",
            "tool_run": item,
        }
        return _format_tool_run_detail_text(item), payload
    suggestions = _subcommand_suggestions(action, ["recent", "summary", "show"])
    text = f"未知 tool-runs 子命令: {action}"
    if suggestions:
        text += "\n你是不是想用: " + ", ".join(f"/tool-runs {item}" for item in suggestions)
    return text, {
        "action": action,
        "error": "unknown_tool_runs_action",
        "suggestions": [f"/tool-runs {item}" for item in suggestions],
    }


async def _permissions(runtime: CommandRuntime, args: str) -> tuple[str, dict]:
    parts = args.split()
    action = parts[0] if parts else "list"
    agent = await runtime.get_agent()
    turn_grants = sorted(str(item) for item in getattr(agent, "_turn_grants", set()) or [])
    legacy_grants = sorted(str(item) for item in getattr(agent, "_destructive_allowed", set()) or [])
    temporary_grants = temporary_grants_snapshot(agent)
    if action == "grants":
        legacy_text = ", ".join(turn_grants or legacy_grants) if (turn_grants or legacy_grants) else "无"
        return "当前 grants: " + legacy_text, {
            "action": "grants",
            "grants": turn_grants or legacy_grants,
            "turn_grants": turn_grants or legacy_grants,
            "temporary_grants": temporary_grants,
        }
    if action not in {"list", "show"}:
        suggestions = _subcommand_suggestions(action, ["list", "show", "grants"])
        text = "用法: /permissions [list|grants]"
        if suggestions:
            text += "\n你是不是想用: " + ", ".join(f"/permissions {item}" for item in suggestions)
        return text, {
            "action": action,
            "error": "unknown_permissions_action",
            "suggestions": [f"/permissions {item}" for item in suggestions],
        }
    policy = getattr(agent, "_execution_policy", None)
    mode = str(getattr(policy, "mode", "") or "")
    permissions = getattr(policy, "permissions", {}) if policy is not None else {}
    lines = [
        f"权限策略: {current_mode(agent)} ({mode or 'standard'})",
        "permissions:",
    ]
    keys = ["default", "read", "search", "write", "bash", "background", "network", "destructive"]
    if isinstance(permissions, dict):
        for key in keys:
            if key in permissions:
                lines.append(f"- {key}: {permissions[key]}")
    ttl = temporary_grant_ttl_seconds(runtime.settings)
    pending = await _call_optional(runtime, "pending_confirmation_status")
    lines.append(f"临时授权 TTL: {_format_ttl(ttl)}")
    lines.append("当前 grants: " + _format_grants_text(temporary_grants, turn_grants or legacy_grants))
    if pending:
        lines.append(f"pending confirm: {pending.get('tool_name', '-')} ({pending.get('permission_category', '-')})")
    return "\n".join(lines), {
        "action": "list",
        "execution_mode": current_mode(agent),
        "policy_mode": mode or "standard",
        "permissions": dict(permissions) if isinstance(permissions, dict) else {},
        "grants": turn_grants or legacy_grants,
        "turn_grants": turn_grants or legacy_grants,
        "temporary_grants": temporary_grants,
        "temporary_grant_ttl_seconds": ttl,
        "pending_confirmation": pending or None,
    }


def _format_grants_text(temporary_grants: list[dict], turn_grants: list[str]) -> str:
    parts: list[str] = []
    for item in temporary_grants:
        parts.append(f"{item['category']} 至 {item['expires_at_iso']}")
    for item in turn_grants:
        parts.append(f"{item} 本轮")
    return ", ".join(parts) if parts else "无"


def _protocol(args: str) -> tuple[str, dict]:
    from personal_agent.conversation import frontend_protocol_schema

    schema = frontend_protocol_schema()
    events = schema.get("events") or {}
    lines = [
        "事件协议",
        f"version: {schema.get('protocol_version')}",
        f"events: {len(events)}",
        "delta events: " + ", ".join(schema.get("delta_event_types") or []),
        "完整 schema: personal-agent protocol schema --json",
    ]
    payload = {
        "action": "schema" if args.strip() == "schema" else "summary",
        "protocol_version": schema.get("protocol_version"),
        "event_count": len(events),
        "delta_event_types": list(schema.get("delta_event_types") or []),
        "events": events if args.strip() == "schema" else {},
    }
    if args.strip() == "schema":
        lines.append("event names: " + ", ".join(sorted(events)))
    return "\n".join(lines), payload


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


async def _steer(runtime: CommandRuntime, args: str) -> str:
    text = str(args or "").strip()
    if not text:
        return "用法: /steer <运行中修正内容>"
    handler = getattr(runtime, "add_steer", None)
    if handler is None:
        return "当前入口不支持 /steer。"
    result = handler(text)
    if hasattr(result, "__await__"):
        result = await result
    return str(result)


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
    return format_commands(runtime)


def _enabled_toolsets(runtime: CommandRuntime) -> list[str] | None:
    settings = getattr(runtime, "settings", None)
    value = getattr(settings, "enabled_toolsets", None)
    return list(value) if value else None


def _format_counts(counts) -> str:
    if not isinstance(counts, dict) or not counts:
        return "无"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _yes(value: bool) -> str:
    return "是" if value else "否"


def _parse_tool_run_options(tokens: list[str]) -> dict:
    result = {
        "limit": 10,
        "all_sessions": False,
    }
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--all":
            result["all_sessions"] = True
        elif token.startswith("--limit="):
            limit = _parse_int(token.split("=", 1)[1])
            if limit is None:
                return {"error": "invalid_limit", "message": f"无效 limit: {token}"}
            result["limit"] = limit
        elif token == "--limit":
            if index + 1 >= len(tokens):
                return {"error": "missing_limit", "message": "用法: --limit <number>"}
            limit = _parse_int(tokens[index + 1])
            if limit is None:
                return {"error": "invalid_limit", "message": f"无效 limit: {tokens[index + 1]}"}
            result["limit"] = limit
            index += 1
        else:
            return {"error": "unknown_option", "message": f"未知参数: {token}"}
        index += 1
    result["limit"] = max(0, int(result["limit"]))
    return result


def _format_tool_runs_recent_text(payload: dict) -> str:
    items = payload.get("items") or []
    scope = "全局" if payload.get("scope") == "all" else f"当前会话 {payload.get('session_key') or '-'}"
    if not items:
        return f"工具运行记录: 无（{scope}）"
    lines = [f"工具运行记录: {len(items)} 条（{scope}）"]
    for item in items:
        lines.append(
            f"- #{item.get('id')} {item.get('tool_name') or '-'} "
            f"{item.get('status') or '-'} {float(item.get('duration') or 0.0):.2f}s "
            f"{_short_text(str(item.get('output_summary') or item.get('error') or ''), 80)}"
        )
    lines.append("用法: /tool-runs show <id>")
    return "\n".join(lines)


def _format_tool_runs_summary_text(payload: dict) -> str:
    scope = "全局" if payload.get("scope") == "all" else f"当前会话 {payload.get('session_key') or '-'}"
    return "\n".join([
        f"工具运行摘要（{scope}）",
        f"inspected: {payload.get('inspected', 0)}",
        f"denied: {payload.get('denied', 0)} failed: {payload.get('failed', 0)} timeouts: {payload.get('timeouts', 0)} truncated: {payload.get('truncated', 0)}",
        f"tool counts: {_format_counts(payload.get('tool_counts'))}",
        f"status counts: {_format_counts(payload.get('status_counts'))}",
        f"category counts: {_format_counts(payload.get('category_counts'))}",
    ])


def _format_tool_run_detail_text(item: dict) -> str:
    output = str(item.get("full_output") or item.get("output_summary") or "")
    return "\n".join([
        f"Tool Run #{item.get('id')}",
        f"tool: {item.get('tool_name') or '-'}",
        f"status: {item.get('status') or '-'}",
        f"category: {item.get('category') or '-'}",
        f"duration: {float(item.get('duration') or 0.0):.2f}s",
        f"session: {item.get('session_key') or '-'}",
        f"turn: {item.get('turn_id') or '-'}",
        f"permission: {item.get('permission_category') or '-'} / {item.get('permission_decision') or '-'}",
        f"mode: {item.get('execution_mode') or '-'}",
        f"input: {_short_text(str(item.get('input_summary') or ''), 160) or '-'}",
        f"output: {_short_text(output, 300) or '-'}",
        f"error: {item.get('error') or '-'}",
    ])


def _parse_int(value: str) -> int | None:
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _subcommand_suggestions(value: str, candidates: list[str]) -> list[str]:
    from difflib import get_close_matches

    return get_close_matches(str(value or ""), candidates, n=3, cutoff=0.65)


def _tool_command_suggestions(value: str) -> list[str]:
    return _subcommand_suggestions(value, ["list", "show"])


def _tool_name_suggestions(value: str, items: list[dict]) -> list[str]:
    from difflib import get_close_matches

    names = sorted(str(item.get("name") or "") for item in items)
    return get_close_matches(str(value or ""), names, n=3, cutoff=0.65)


def _tool_argument_choices(
    runtime: CommandRuntime,
    *,
    query: str = "",
    limit: int = 20,
) -> list[dict]:
    from personal_agent.tools.registry import tool_registry

    needle = str(query or "").strip().lower()
    result: list[dict] = []
    for item in tool_registry.catalog(_enabled_toolsets(runtime)):
        name = str(item.get("name") or "")
        if needle and needle not in name.lower():
            continue
        description_parts = [
            str(item.get("permission_category") or "").strip(),
            str(item.get("risk_level") or "").strip(),
        ]
        description = " · ".join(part for part in description_parts if part)
        result.append({
            "value": name,
            "label": name,
            "description": description,
            "append_space": False,
        })
        if len(result) >= max(0, int(limit)):
            break
    return result


async def _session_argument_choices(
    runtime: CommandRuntime,
    *,
    query: str = "",
    limit: int = 20,
) -> list[dict]:
    query_service = getattr(getattr(runtime, "conversation_service", None), "queries", None)
    if query_service is None:
        return []
    source = getattr(runtime, "source", None)
    platform = str(getattr(source, "platform", "") or "")
    user_id = str(getattr(source, "user_id", "") or "")
    current_key = str(getattr(runtime, "session_key", "") or "")
    if not platform or not user_id:
        return []
    data = await query_service.list_sessions(
        platform=platform,
        user_id=user_id,
        current_key=current_key,
        limit=max(0, int(limit)),
    )
    needle = str(query or "").strip().lower()
    result: list[dict] = []
    for item in data.get("items") or []:
        session_key = str(item.get("session_key") or "")
        name = _session_name_from_key(session_key, platform=platform, user_id=user_id)
        if needle and needle not in name.lower() and needle not in session_key.lower():
            continue
        message_count = int(item.get("message_count") or 0)
        result.append({
            "value": name,
            "label": name,
            "description": f"{message_count} messages",
            "append_space": False,
        })
    return result


def _session_name_from_key(session_key: str, *, platform: str, user_id: str) -> str:
    prefix = f"{platform}:"
    suffix = f":{user_id}"
    if session_key.startswith(prefix) and session_key.endswith(suffix):
        return session_key[len(prefix):-len(suffix)]
    return session_key
