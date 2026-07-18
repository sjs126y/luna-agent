"""Slash command metadata registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any

SLASH_COMMAND_REGISTRY_VERSION = 1


@dataclass(frozen=True)
class ArgumentChoiceSpec:
    value: str
    label: str
    description: str = ""
    append_space: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "label": self.label,
            "description": self.description,
            "append_space": self.append_space,
        }


@dataclass(frozen=True)
class CommandArgumentSpec:
    name: str
    kind: str
    choices: tuple[ArgumentChoiceSpec, ...] = ()
    provider: str = ""
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "choices": [choice.as_dict() for choice in self.choices],
            "provider": self.provider,
            "required": self.required,
        }


@dataclass(frozen=True)
class CommandSpec:
    name: str
    summary: str
    usage: str
    category: str = "general"
    aliases: tuple[str, ...] = ()
    children: tuple["CommandSpec", ...] = field(default_factory=tuple)
    arguments: tuple[CommandArgumentSpec, ...] = field(default_factory=tuple)
    available_in: tuple[str, ...] = ("chat", "gateway")
    mutates_state: bool = False
    requires_agent: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "usage": self.usage,
            "category": self.category,
            "aliases": list(self.aliases),
            "available_in": list(self.available_in),
            "mutates_state": self.mutates_state,
            "requires_agent": self.requires_agent,
            "arguments": [argument.as_dict() for argument in self.arguments],
            "children": [child.as_dict() for child in self.children],
        }


_MODE_ARGUMENT = CommandArgumentSpec(
    name="mode",
    kind="choice",
    choices=(
        ArgumentChoiceSpec("Read Only", "Read Only", "只读"),
        ArgumentChoiceSpec("Ask First", "Ask First", "执行前确认"),
        ArgumentChoiceSpec("Local Auto", "Local Auto", "工作区自动"),
        ArgumentChoiceSpec("Full Auto", "Full Auto", "全自动"),
    ),
)

_DENY_ARGUMENT = CommandArgumentSpec(
    name="scope",
    kind="choice",
    choices=(
        ArgumentChoiceSpec("all", "all", "当前会话全部限时授权"),
    ),
)

_TOOL_NAME_ARGUMENT = CommandArgumentSpec(
    name="name",
    kind="dynamic",
    provider="tools",
)

_SESSION_NAME_ARGUMENT = CommandArgumentSpec(
    name="name",
    kind="dynamic",
    provider="sessions",
    required=False,
)

_STEER_TEXT_ARGUMENT = CommandArgumentSpec(
    name="text",
    kind="text",
)

_ACTIVITY_SCOPE_ARGUMENT = CommandArgumentSpec(
    name="scope",
    kind="choice",
    choices=(
        ArgumentChoiceSpec("agents", "agents", "子 agent"),
        ArgumentChoiceSpec("processes", "processes", "后台任务"),
        ArgumentChoiceSpec("gateway", "gateway", "Gateway agent"),
    ),
    required=False,
)

_ACTIVITY_AGENT_ARGUMENT = CommandArgumentSpec(
    name="id",
    kind="dynamic",
    provider="activity_agents",
    required=False,
)

_ACTIVITY_PROCESS_ARGUMENT = CommandArgumentSpec(
    name="id",
    kind="dynamic",
    provider="activity_processes",
    required=False,
)

_ACTIVITY_GATEWAY_ARGUMENT = CommandArgumentSpec(
    name="id",
    kind="dynamic",
    provider="activity_gateway",
    required=False,
)


CORE_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("new", "重置当前会话", "/new", category="session", mutates_state=True),
    CommandSpec(
        "session",
        "管理会话",
        "/session [current|list|switch <name>|rename <name>|delete [name]]",
        category="session",
        children=(
            CommandSpec("current", "显示当前会话", "/session current", category="session"),
            CommandSpec("list", "列出会话", "/session list", category="session"),
            CommandSpec(
                "switch",
                "切换会话",
                "/session switch <name>",
                category="session",
                arguments=(_SESSION_NAME_ARGUMENT,),
            ),
            CommandSpec("rename", "重命名会话", "/session rename <name>", category="session"),
            CommandSpec(
                "delete",
                "删除会话",
                "/session delete [name]",
                category="session",
                arguments=(_SESSION_NAME_ARGUMENT,),
            ),
        ),
    ),
    CommandSpec("usage", "查看当前会话上下文预算", "/usage", category="runtime"),
    CommandSpec("export", "导出当前会话 JSONL", "/export", category="session"),
    CommandSpec(
        "deny",
        "撤销当前会话的全部限时工具与资源授权",
        "/deny all",
        category="execution",
        mutates_state=True,
        arguments=(_DENY_ARGUMENT,),
    ),
    CommandSpec(
        "mode",
        "查看或切换执行模式",
        "/mode [Read Only|Ask First|Local Auto|Full Auto]",
        category="execution",
        mutates_state=True,
        requires_agent=True,
        children=(
            CommandSpec("list", "列出执行模式", "/mode list", category="execution"),
            CommandSpec("show", "显示当前执行模式", "/mode show", category="execution"),
            CommandSpec(
                "set",
                "切换执行模式",
                "/mode set <mode>",
                category="execution",
                arguments=(_MODE_ARGUMENT,),
                mutates_state=True,
            ),
        ),
    ),
    CommandSpec(
        "permissions",
        "查看当前权限策略和 grants",
        "/permissions [list|grants]",
        category="execution",
        requires_agent=True,
        children=(
            CommandSpec("list", "列出权限策略", "/permissions list", category="execution"),
            CommandSpec("grants", "列出当前 grants", "/permissions grants", category="execution"),
        ),
    ),
    CommandSpec("stop", "停止当前处理", "/stop", category="runtime", mutates_state=True, requires_agent=True),
    CommandSpec(
        "steer",
        "运行中修正当前任务",
        "/steer <text>",
        category="runtime",
        mutates_state=True,
        arguments=(_STEER_TEXT_ARGUMENT,),
    ),
    CommandSpec(
        "agents",
        "查看子 agent 运行记录",
        "/agents [list [limit]|show <run_id>|clear]",
        category="agents",
        aliases=("agent-runs",),
        children=(
            CommandSpec("list", "列出子 agent 运行记录", "/agents list [limit]", category="agents"),
            CommandSpec("show", "查看子 agent 运行详情", "/agents show <run_id>", category="agents"),
            CommandSpec("clear", "清理子 agent 运行记录", "/agents clear", category="agents", mutates_state=True),
        ),
    ),
    CommandSpec(
        "activity",
        "查看子 agent、后台任务和 Gateway agent 活动",
        "/activity [agents|processes|gateway] [id]",
        category="runtime",
        arguments=(_ACTIVITY_SCOPE_ARGUMENT,),
        children=(
            CommandSpec(
                "agents",
                "查看子 agent 活动",
                "/activity agents [id]",
                category="runtime",
                arguments=(_ACTIVITY_AGENT_ARGUMENT,),
            ),
            CommandSpec(
                "processes",
                "查看后台任务活动",
                "/activity processes [id]",
                category="runtime",
                arguments=(_ACTIVITY_PROCESS_ARGUMENT,),
            ),
            CommandSpec(
                "gateway",
                "查看 Gateway agent 活动",
                "/activity gateway [id]",
                category="runtime",
                arguments=(_ACTIVITY_GATEWAY_ARGUMENT,),
            ),
        ),
    ),
    CommandSpec(
        "memory",
        "查看和管理记忆",
        "/memory [list|search <query>|show <id>|delete <id>|doctor]",
        category="memory",
        children=(
            CommandSpec("doctor", "查看 memory 诊断", "/memory doctor", category="memory"),
            CommandSpec("list", "列出记忆", "/memory list", category="memory"),
            CommandSpec("search", "搜索记忆", "/memory search <query>", category="memory"),
            CommandSpec("show", "查看记忆详情", "/memory show <id>", category="memory"),
            CommandSpec("delete", "删除记忆", "/memory delete <id>", category="memory", mutates_state=True),
        ),
    ),
    CommandSpec(
        "tools",
        "查看可用工具",
        "/tools [list|show <name>]",
        category="tools",
        children=(
            CommandSpec("list", "列出工具总览", "/tools list", category="tools"),
            CommandSpec(
                "show",
                "查看工具详情",
                "/tools show <name>",
                category="tools",
                arguments=(_TOOL_NAME_ARGUMENT,),
            ),
        ),
    ),
    CommandSpec(
        "tool-runs",
        "查询工具运行记录",
        "/tool-runs [recent|summary|show <id>] [--all] [--limit N]",
        category="tools",
        children=(
            CommandSpec("recent", "查看最近工具运行", "/tool-runs recent [--all] [--limit N]", category="tools"),
            CommandSpec("summary", "查看工具运行摘要", "/tool-runs summary [--all] [--limit N]", category="tools"),
            CommandSpec("show", "查看工具运行详情", "/tool-runs show <id>", category="tools"),
        ),
    ),
    CommandSpec(
        "protocol",
        "查看前端事件协议摘要",
        "/protocol [schema]",
        category="runtime",
        children=(CommandSpec("schema", "查看协议 schema 摘要", "/protocol schema", category="runtime"),),
    ),
    CommandSpec(
        "plugins",
        "管理运行中的插件",
        "/plugins [list|info|install|reload|enable|disable|active|rollback|uninstall]",
        category="runtime",
        mutates_state=True,
        children=(
            CommandSpec("list", "列出插件", "/plugins list", category="runtime"),
            CommandSpec("info", "查看插件", "/plugins info <key>", category="runtime"),
            CommandSpec("install", "安装本地插件", "/plugins install <path>", category="runtime", mutates_state=True),
            CommandSpec("reload", "热重载插件", "/plugins reload <key>", category="runtime", mutates_state=True),
            CommandSpec("enable", "启用插件", "/plugins enable <key>", category="runtime", mutates_state=True),
            CommandSpec("disable", "禁用插件", "/plugins disable <key>", category="runtime", mutates_state=True),
            CommandSpec(
                "active",
                "控制主动运行器",
                "/plugins active <key> <on|off|restart>",
                category="runtime",
                mutates_state=True,
            ),
            CommandSpec("rollback", "回滚插件包", "/plugins rollback <key> <digest>", category="runtime", mutates_state=True),
            CommandSpec("uninstall", "卸载插件", "/plugins uninstall <key> [--purge-data]", category="runtime", mutates_state=True),
        ),
    ),
    CommandSpec("commands", "列出 slash commands", "/commands [json|<name>]", category="help"),
    CommandSpec("help", "显示帮助", "/help", category="help"),
)

CORE_COMMAND_NAMES: frozenset[str] = frozenset(
    name
    for spec in CORE_COMMAND_SPECS
    for name in (spec.name, *spec.aliases)
)


def list_command_specs(runtime: Any | None = None) -> list[CommandSpec]:
    environment = _runtime_environment(runtime)
    return [
        spec
        for spec in CORE_COMMAND_SPECS
        if environment is None or environment in spec.available_in
    ]


def command_specs_as_dict(runtime: Any | None = None) -> dict[str, Any]:
    return {
        "version": SLASH_COMMAND_REGISTRY_VERSION,
        "commands": [spec.as_dict() for spec in list_command_specs(runtime)],
        "plugin_commands": _plugin_commands_as_dict(runtime),
    }


def format_commands(runtime: Any | None = None) -> str:
    lines = ["可用命令:"]
    for spec in sorted(list_command_specs(runtime), key=lambda item: (item.category, item.name)):
        lines.append(f"/{spec.name} - {spec.summary}")
    if runtime is not None and "cli" in _plugin_command_scopes(runtime):
        lines.append('""" - 进入多行输入，再输入 """ 提交，/cancel 取消')
    lines.append("/<skill-name> [message] - 加载技能后发送消息")
    lines.append("exit / quit / 空行 - 退出 CLI")
    plugin_lines = plugin_command_help_lines(runtime)
    if plugin_lines:
        lines.extend(["", "插件命令:", *plugin_lines])
    return "\n".join(lines)


def find_command_spec(name: str) -> CommandSpec | None:
    key = str(name or "").lstrip("/").strip()
    for spec in CORE_COMMAND_SPECS:
        if key == spec.name or key in spec.aliases:
            return spec
    return None


def format_command_detail(name: str, runtime: Any | None = None) -> str:
    spec = find_command_spec(name)
    if spec is not None and spec not in list_command_specs(runtime):
        spec = None
    if spec is None:
        return f"未找到命令: {name}"
    lines = [
        f"/{spec.name}",
        spec.summary,
        f"用法: {spec.usage}",
        f"类别: {spec.category}",
    ]
    if spec.aliases:
        lines.append("别名: " + ", ".join(f"/{alias}" for alias in spec.aliases))
    if spec.children:
        lines.append("子命令:")
        lines.extend(f"- {child.usage} - {child.summary}" for child in spec.children)
    return "\n".join(lines)


def plugin_command_help_lines(runtime: Any | None) -> list[str]:
    if runtime is None or getattr(runtime, "plugin_manager", None) is None:
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


def _plugin_commands_as_dict(runtime: Any | None) -> list[dict[str, Any]]:
    if runtime is None or getattr(runtime, "plugin_manager", None) is None:
        return []
    commands = getattr(runtime.plugin_manager, "commands", {})
    if not isinstance(commands, dict):
        return []
    scopes = set(_plugin_command_scopes(runtime))
    result = []
    for name, entry in sorted(commands.items()):
        entry_scope = getattr(entry, "scope", "slash")
        if entry_scope != "both" and entry_scope not in scopes:
            continue
        result.append({
            "name": name,
            "summary": getattr(entry, "description", "") or "插件命令",
            "usage": f"/{name}",
            "category": "plugin",
            "scope": entry_scope,
            "plugin_key": getattr(entry, "plugin_key", ""),
            "available_in": _plugin_available_in(entry_scope),
            "mutates_state": False,
            "requires_agent": False,
        })
    return result


def suggest_command_names(runtime: Any | None, name: str, *, limit: int = 3) -> list[str]:
    key = str(name or "").lstrip("/").strip()
    if not key:
        return []
    candidates: set[str] = set()
    for spec in list_command_specs(runtime):
        candidates.add(spec.name)
        candidates.update(spec.aliases)
    for item in _plugin_commands_as_dict(runtime):
        candidates.add(str(item.get("name") or ""))
    matches = get_close_matches(key, sorted(candidates), n=limit, cutoff=0.72)
    return [f"/{item}" for item in matches]


def _plugin_command_scopes(runtime: Any) -> tuple[str, ...]:
    scopes = getattr(runtime, "plugin_command_scopes", ("slash",))
    if isinstance(scopes, str):
        scopes = (scopes,)
    cleaned = tuple(scope for scope in scopes if scope in {"slash", "cli", "both"})
    return cleaned or ("slash",)


def _runtime_environment(runtime: Any | None) -> str | None:
    if runtime is None:
        return None
    source = getattr(runtime, "source", None)
    platform = str(getattr(source, "platform", "") or "")
    if platform == "cli":
        return "chat"
    return "gateway"


def _plugin_available_in(scope: str) -> list[str]:
    if scope == "cli":
        return ["chat"]
    if scope == "both":
        return ["chat", "gateway"]
    return ["chat", "gateway"]
