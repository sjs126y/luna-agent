"""Typer CLI for Personal Agent."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any, Optional

import typer

from personal_agent.config import Settings
from personal_agent.context_budget import build_context_budget
from personal_agent.main import _run_cli, boot
from personal_agent.plugins.manager import PluginManager

app = typer.Typer(help="Personal Agent")
plugins_app = typer.Typer(help="Manage plugins")
tokens_app = typer.Typer(help="Estimate token/context usage")
agents_app = typer.Typer(help="Run controlled agent helpers")

app.add_typer(plugins_app, name="plugins")
app.add_typer(tokens_app, name="tokens")
app.add_typer(agents_app, name="agents")


@app.command()
def chat(message: str = typer.Argument("Hello")) -> None:
    """Run one debug chat turn."""
    _run_cli(message)


@app.command()
def serve() -> None:
    """Run the platform gateway service."""
    asyncio.run(boot())


@app.command()
def doctor(json_output: bool = typer.Option(False, "--json", help="输出 JSON。")) -> None:
    """Show system diagnostics."""
    report = build_doctor_report()
    if json_output:
        typer.echo(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        typer.echo(format_doctor_report(report))


@plugins_app.command("list")
def plugins_list(
    load: bool = typer.Option(False, help="加载已启用的非延迟插件后再显示。"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON。"),
) -> None:
    manager = _plugin_manager()
    if load:
        manager.load_enabled()
    reports = [manager.doctor_plugin(plugin.key) for plugin in manager.list_plugins()]
    if json_output:
        typer.echo(json.dumps(reports, indent=2, ensure_ascii=False))
    else:
        typer.echo(format_plugin_list(reports))


@plugins_app.command("info")
def plugins_info(
    key: str,
    load: bool = typer.Option(False, help="先加载该插件以显示注册项。"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON。"),
) -> None:
    manager = _plugin_manager()
    if load:
        manager.load_plugin(key)
    report = manager.doctor_plugin(key)
    if json_output:
        typer.echo(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        typer.echo(format_plugin_report(report, include_traceback=False))


@plugins_app.command("enable")
def plugins_enable(key: str) -> None:
    manager = _plugin_manager()
    plugin = manager.enable_plugin(key)
    typer.echo(f"已启用插件: {plugin.key}")


@plugins_app.command("disable")
def plugins_disable(key: str) -> None:
    manager = _plugin_manager()
    plugin = manager.disable_plugin(key)
    typer.echo(f"已禁用插件: {plugin.key}")


@plugins_app.command("doctor")
def plugins_doctor(
    key: str,
    json_output: bool = typer.Option(False, "--json", help="输出 JSON。"),
) -> None:
    manager = _plugin_manager()
    plugin = manager.load_plugin(key)
    report = manager.doctor_plugin(plugin.key)
    if json_output:
        typer.echo(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        typer.echo(format_plugin_report(report, include_traceback=True))


@tokens_app.command("estimate")
def tokens_estimate(
    text: str = typer.Argument(""),
    model: str = typer.Option("", help="Model name for tokenizer selection."),
) -> None:
    from personal_agent.llm.token_counter import estimate_tokens

    typer.echo(estimate_tokens(text, model))


@tokens_app.command("session")
def tokens_session(
    session_json: Optional[Path] = typer.Argument(None, help="Optional JSON file with messages."),
    model: str = typer.Option("", help="Model name for tokenizer selection."),
    context_limit: int = typer.Option(0, help="Context limit override."),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON。"),
) -> None:
    async def _run() -> None:
        settings = Settings()
        manager = _plugin_manager(settings)
        manager.load_enabled()

        from personal_agent.llm.provider import _detect_context_window
        from personal_agent.skills.registry import skill_registry
        from personal_agent.tools.registry import tool_registry

        messages: list[dict] = []
        if session_json is not None:
            messages.extend(json.loads(session_json.read_text(encoding="utf-8")))

        effective_model = model or settings.llm_model
        effective_limit = context_limit or _detect_context_window(effective_model)
        budget = await build_context_budget(
            messages=messages,
            settings=settings,
            model=effective_model,
            context_limit=effective_limit,
            tools=tool_registry.get_definitions(
                enabled_toolsets=settings.enabled_toolsets,
                quiet_mode=True,
            ),
            skills_summary=skill_registry.get_summaries(),
        )
        data = budget.as_dict()
        if json_output:
            typer.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            typer.echo(format_token_budget(data))

    asyncio.run(_run())


@agents_app.command("run")
def agents_run(prompt: str) -> None:
    typer.echo(
        "agents run 需要在活跃 agent 会话中使用已配置的运行时；"
        "请在 chat/serve 中使用 delegate_task 工具。"
    )
    typer.echo(prompt)


@agents_app.command("workflow")
def agents_workflow(name: str, args: str = "{}") -> None:
    async def _run() -> None:
        from personal_agent.workflow.engine import run_workflow_tool

        typer.echo(await run_workflow_tool(name, args))

    asyncio.run(_run())


def _plugin_manager(settings: Settings | None = None) -> PluginManager:
    settings = settings or Settings()
    manager = PluginManager(settings)
    manager.discover()
    return manager


def build_doctor_report(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or Settings()
    manager = _plugin_manager(settings)
    manager.load_enabled()

    from personal_agent.llm.token_counter import tokenizer_status

    plugins = [manager.doctor_plugin(plugin.key) for plugin in manager.list_plugins()]
    sandbox_roots = [
        {"path": str(root), "exists": Path(root).exists()}
        for root in settings.sandbox_roots
    ]
    mcp_servers = []
    for server in settings.mcp_servers:
        command = str(server.get("command", ""))
        mcp_servers.append({
            "name": server.get("name", command or "unknown"),
            "command": command,
            "enabled": bool(server.get("enabled", True)),
            "command_found": bool(command and shutil.which(command)),
        })

    platform_plugins = [
        {
            "key": plugin["key"],
            "name": plugin["name"],
            "status": plugin["status"],
            "missing_env": plugin["missing_env"],
            "enabled": plugin["enabled"],
        }
        for plugin in plugins
        if plugin.get("kind") == "platform"
    ]

    return {
        "data_dir": str(settings.agent_data_dir),
        "log_level": settings.log_level,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "mcp_enabled": settings.mcp_enabled,
        "sandbox": {
            "roots": sandbox_roots,
            "blocked_count": len(settings.sandbox_blocked),
            "bash_work_dir": str(settings.bash_work_dir),
        },
        "mcp_servers": mcp_servers,
        "platforms": platform_plugins,
        "plugins": plugins,
        "tokenizer": tokenizer_status(),
    }


def format_doctor_report(report: dict[str, Any]) -> str:
    lines = [
        "Personal Agent 诊断",
        f"数据目录: {report['data_dir']}",
        f"日志级别: {report['log_level']}",
        f"LLM: {report['llm_provider']} / {report['llm_model']}",
        f"MCP: {_yes(report['mcp_enabled'])}",
        "",
        "Sandbox:",
    ]
    for root in report["sandbox"]["roots"]:
        lines.append(f"  - {root['path']} [{_status(root['exists'])}]")
    lines.append(f"  blocked 规则: {report['sandbox']['blocked_count']}")
    lines.append(f"  bash 工作目录: {report['sandbox']['bash_work_dir']}")

    lines.extend(["", "MCP 服务器:"])
    if report["mcp_servers"]:
        for server in report["mcp_servers"]:
            state = "禁用" if not server["enabled"] else _status(server["command_found"])
            lines.append(f"  - {server['name']}: {server['command'] or '-'} [{state}]")
    else:
        lines.append("  - 无")

    lines.extend(["", "平台配置:"])
    if report["platforms"]:
        for platform in report["platforms"]:
            missing = _list_or_none(platform["missing_env"])
            lines.append(
                f"  - {platform['key']}: 状态={platform['status']} "
                f"启用={_yes(platform['enabled'])} 缺失环境变量={missing}"
            )
    else:
        lines.append("  - 无")

    tokenizer = report["tokenizer"]
    lines.extend([
        "",
        "Tokenizer:",
        f"  tiktoken 可用: {_yes(tokenizer['tiktoken_available'])}",
        f"  fallback 生效: {_yes(tokenizer['fallback_active'])}",
        f"  默认 encoding: {tokenizer['default_encoding']}",
        f"  已缓存 encoding: {_list_or_none(tokenizer['cached_encodings'].keys())}",
        "",
        "插件:",
    ])
    lines.append(format_plugin_list(report["plugins"]))
    error_plugins = [plugin for plugin in report["plugins"] if plugin["status"] == "ERROR"]
    if error_plugins:
        lines.extend(["", "错误插件:"])
        for plugin in error_plugins:
            lines.append(f"  - {plugin['key']}: {plugin['error'] or plugin['entrypoint_error']}")
    return "\n".join(lines)


def format_plugin_list(reports: list[dict[str, Any]]) -> str:
    lines = ["插件\t状态\t启用\t延迟\t注册项\t错误"]
    for report in reports:
        lines.append(
            f"{report['key']}\t{report['status']}\t{_yes(report['enabled'])}\t"
            f"{_yes(report['deferred'])}\t{_registration_summary(report['registered'])}\t"
            f"{report['error'] or report['entrypoint_error'] or '-'}"
        )
    return "\n".join(lines)


def format_plugin_report(report: dict[str, Any], *, include_traceback: bool) -> str:
    lines = [
        f"插件: {report['key']}",
        f"名称: {report['name']} ({report['version']})",
        f"描述: {report['description'] or '-'}",
        f"类型: {report['kind']}  来源: {report['source']}",
        f"入口: {report['entrypoint']} [{_status(report['entrypoint_importable'])}]",
        f"启用: {_yes(report['enabled'])}  默认启用: {_yes(report['enabled_by_default'])}  延迟加载: {_yes(report['deferred'])}",
        f"状态: {report['status']}",
        f"提供能力: {_list_or_none(report['provides'])}",
        f"需要环境变量: {_list_or_none(report['requires_env'])}",
        f"缺失环境变量: {_list_or_none(report['missing_env'])}",
        f"注册数量: {_registration_summary(report['registered'])}",
        "注册项:",
    ]
    for group, items in report["registered_items"].items():
        lines.append(f"  {group}: {_list_or_none(items)}")
    if report["error"] or report["entrypoint_error"]:
        lines.extend([
            "",
            f"错误: {report['error'] or report['entrypoint_error']}",
        ])
    if include_traceback and report["error_traceback"]:
        lines.extend(["", "Traceback:", report["error_traceback"].strip()])
    return "\n".join(lines)


def format_token_budget(data: dict[str, Any]) -> str:
    lines = [
        "上下文预算估算",
        f"已用: {data['used']:,} / {data['context_limit']:,} tokens ({data['percent']}%)",
        f"剩余: {data['remaining_context']:,}",
        f"system prompt: {data['system_prompt']:,}",
        f"history messages: {data['history_messages']:,}",
        f"tools schema: {data['tools_schema']:,}",
        f"skills: {data['skills']:,}",
        f"memory injections: {data['memory_injections']:,}",
        f"MCP tools: {data['mcp_tools']:,}",
    ]
    if data.get("compression_threshold"):
        marker = " (已达到)" if data.get("over_compression_threshold") else ""
        lines.append(f"compression threshold: {data['compression_threshold']:,}{marker}")
    return "\n".join(lines)


def _registration_summary(counts: dict[str, int]) -> str:
    parts = [
        f"tools={counts.get('tools', 0)}",
        f"skills={counts.get('skills', 0)}",
        f"workflows={counts.get('workflows', 0)}",
        f"platforms={counts.get('platforms', 0)}",
        f"hooks={counts.get('hooks', 0)}",
        f"commands={counts.get('commands', 0)}",
    ]
    return " ".join(parts)


def _yes(value: bool) -> str:
    return "是" if value else "否"


def _status(ok: bool) -> str:
    return "正常" if ok else "异常"


def _list_or_none(items) -> str:
    values = list(items or [])
    return ", ".join(str(item) for item in values) if values else "无"


def run() -> None:
    app()
