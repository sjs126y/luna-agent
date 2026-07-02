"""Typer CLI for Personal Agent."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

import typer

from personal_agent.config import Settings
from personal_agent.context_budget import build_context_budget
from personal_agent.cli_chat import run_cli_once_sync, run_cli_repl_sync
from personal_agent.main import boot
from personal_agent.plugins.manager import PluginManager

app = typer.Typer(help="Personal Agent")
plugins_app = typer.Typer(help="Manage plugins")
tokens_app = typer.Typer(help="Estimate token/context usage")
agents_app = typer.Typer(help="Run controlled agent helpers")

app.add_typer(plugins_app, name="plugins")
app.add_typer(tokens_app, name="tokens")
app.add_typer(agents_app, name="agents")


@app.command()
def chat(
    message: str = typer.Argument("", help="可选：只运行一轮消息后退出。"),
    once: str = typer.Option("", "--once", "-o", help="只运行一轮消息后退出。"),
    session: str = typer.Option("default", "--session", "-s", help="CLI 会话名。"),
) -> None:
    """Interactive multi-turn chat loop."""
    one_shot = once or message
    try:
        if one_shot:
            run_cli_once_sync(one_shot, session_name=session)
        else:
            run_cli_repl_sync(session_name=session)
    except Exception as exc:
        _exit_error(f"CLI 运行失败: {exc}")


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


@agents_app.command("list")
def agents_list(
    limit: int = typer.Option(20, "--limit", "-n", help="显示最近 N 条记录。"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON。"),
) -> None:
    _load_agent_run_store()
    from personal_agent.plugins.builtin.tools.builtin.delegate import (
        format_agent_runs,
        list_agent_runs,
    )

    if json_output:
        typer.echo(_json_dumps(list_agent_runs(limit=limit)))
    else:
        typer.echo(format_agent_runs(limit=limit))


@agents_app.command("show")
def agents_show(
    run_id: str,
    json_output: bool = typer.Option(False, "--json", help="输出 JSON。"),
) -> None:
    _load_agent_run_store()
    from personal_agent.plugins.builtin.tools.builtin.delegate import (
        format_agent_run,
        get_agent_run,
    )

    run = get_agent_run(run_id)
    if run is None:
        _exit_error(f"未找到子 agent 运行记录: {run_id}")
    if json_output:
        typer.echo(_json_dumps(_agent_run_to_dict(run)))
    else:
        typer.echo(format_agent_run(run_id))


@agents_app.command("export")
def agents_export(
    run_id: str,
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="导出 JSON 文件路径。"),
) -> None:
    settings = _load_agent_run_store()
    from personal_agent.plugins.builtin.tools.builtin.delegate import get_agent_run

    run = get_agent_run(run_id)
    if run is None:
        _exit_error(f"未找到子 agent 运行记录: {run_id}")
    target = output or settings.agent_data_dir / "exports" / f"agent_run_{run_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_json_dumps(_agent_run_to_dict(run)) + "\n", encoding="utf-8")
    typer.echo(f"已导出子 agent 运行记录: {target}")


@agents_app.command("clear")
def agents_clear() -> None:
    _load_agent_run_store()
    from personal_agent.plugins.builtin.tools.builtin.delegate import clear_agent_runs

    count = clear_agent_runs()
    typer.echo(f"已清理 {count} 条子 agent 运行记录。")


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


def _load_agent_run_store(settings: Settings | None = None) -> Settings:
    settings = settings or Settings()
    from personal_agent.plugins.builtin.tools.builtin.delegate import load_agent_runs

    load_agent_runs(settings.agent_data_dir / "agent_runs.jsonl")
    return settings


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
    issues = _doctor_issues(report)
    plugin_summary = _plugin_status_summary(report["plugins"])
    lines = [
        "Personal Agent 诊断",
        f"总体状态: {'需要注意' if issues else '正常'}",
        f"数据目录: {report['data_dir']}",
        f"日志级别: {report['log_level']}",
        f"LLM: {report['llm_provider']} / {report['llm_model']}",
        f"MCP: {_yes(report['mcp_enabled'])}",
        (
            "插件概览: "
            f"总数={plugin_summary['total']} "
            f"已加载={plugin_summary['loaded']} "
            f"延迟={plugin_summary['deferred']} "
            f"禁用={plugin_summary['disabled']} "
            f"错误={plugin_summary['error']}"
        ),
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
    lines.append(format_plugin_list(report["plugins"], include_summary=False))
    lines.extend(["", "需要注意:"])
    if issues:
        lines.extend(f"  - {issue}" for issue in issues)
    else:
        lines.append("  - 无")
    return "\n".join(lines)


def format_plugin_list(reports: list[dict[str, Any]], *, include_summary: bool = True) -> str:
    summary = _plugin_status_summary(reports)
    lines: list[str] = []
    if include_summary:
        lines.append(
            "插件概览: "
            f"总数={summary['total']} "
            f"已加载={summary['loaded']} "
            f"延迟={summary['deferred']} "
            f"禁用={summary['disabled']} "
            f"错误={summary['error']}"
        )
    for group, grouped_reports in _group_plugins(reports).items():
        if lines:
            lines.append("")
        lines.append(f"{group}:")
        for report in grouped_reports:
            lines.append(
                f"  - {report['key']} [{report['status']}] "
                f"启用={_yes(report['enabled'])} "
                f"延迟={_yes(report['deferred'])} "
                f"注册={_registration_summary(report['registered'])} "
                f"问题={_plugin_issue_summary(report)}"
            )
    return "\n".join(lines)


def format_plugin_report(report: dict[str, Any], *, include_traceback: bool) -> str:
    diagnostics = _plugin_diagnostics(report)
    manifest_error = report.get("manifest_error") or ""
    deferred_reason = report.get("deferred_reason") or ""
    lines = [
        f"插件: {report['key']}",
        f"名称: {report['name']} ({report['version']})",
        f"描述: {report['description'] or '-'}",
        f"类型: {report['kind']}  来源: {report['source']}",
        f"路径: {report.get('path') or '-'}",
        f"Manifest: {_status(report.get('manifest_valid', True))}",
        f"入口: {report['entrypoint']} [{_status(report['entrypoint_importable'])}]",
        f"启用: {_yes(report['enabled'])}  默认启用: {_yes(report['enabled_by_default'])}  延迟加载: {_yes(report['deferred'])}",
        f"状态: {report['status']}",
        f"提供能力: {_list_or_none(report['provides'])}",
        f"需要环境变量: {_list_or_none(report['requires_env'])}",
        f"缺失环境变量: {_list_or_none(report['missing_env'])}",
        f"注册数量: {_registration_summary(report['registered'])}",
        "诊断:",
    ]
    if manifest_error:
        lines.insert(6, f"Manifest 错误: {manifest_error}")
    if deferred_reason:
        lines.insert(10 if manifest_error else 9, f"延迟原因: {deferred_reason}")
    lines.extend(f"  - {item}" for item in diagnostics)
    lines.append("注册项:")
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


def _doctor_issues(report: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for root in report["sandbox"]["roots"]:
        if not root["exists"]:
            issues.append(f"Sandbox root 不存在: {root['path']}")

    for server in report["mcp_servers"]:
        if server["enabled"] and not server["command_found"]:
            issues.append(f"MCP 服务器 {server['name']} 的命令不可用: {server['command'] or '-'}")

    tokenizer = report["tokenizer"]
    if tokenizer.get("fallback_active"):
        issues.append("Tokenizer 正在使用 fallback 估算，token 数可能不够精确。")

    for plugin in report["plugins"]:
        diagnostics = [
            item
            for item in _plugin_diagnostics(plugin)
            if item != "当前无明显问题。"
        ]
        for item in diagnostics:
            if item.startswith("延迟加载") or item.startswith("建议:"):
                continue
            issues.append(f"插件 {plugin['key']}: {item}")
    return issues


def _plugin_status_summary(reports: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(reports),
        "loaded": sum(1 for report in reports if report["status"] == "LOADED"),
        "deferred": sum(1 for report in reports if report["status"] == "DEFERRED"),
        "disabled": sum(1 for report in reports if not report["enabled"] or report["status"] == "DISABLED"),
        "error": sum(1 for report in reports if report["status"] == "ERROR"),
    }


def _group_plugins(reports: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for report in reports:
        groups.setdefault(_plugin_group_label(report), []).append(report)
    return {
        group: sorted(items, key=lambda item: item["key"])
        for group, items in groups.items()
    }


def _plugin_group_label(report: dict[str, Any]) -> str:
    key = str(report.get("key", ""))
    kind = str(report.get("kind", ""))
    source = str(report.get("source", ""))
    if key.startswith("platforms/") or kind == "platform":
        return "平台插件"
    if key.startswith("memory/"):
        return "记忆插件"
    if key.startswith("workflows/"):
        return "工作流插件"
    if source == "builtin" or key.startswith("builtin/"):
        return "内置插件"
    return "用户插件"


def _plugin_issue_summary(report: dict[str, Any]) -> str:
    issues = [
        item
        for item in _plugin_diagnostics(report)
        if item != "当前无明显问题。" and not item.startswith("延迟加载") and not item.startswith("建议:")
    ]
    return "；".join(issues) if issues else "-"


def _plugin_diagnostics(report: dict[str, Any]) -> list[str]:
    diagnostics: list[str] = []
    manifest_valid = report.get("manifest_valid", True)
    manifest_error = report.get("manifest_error") or ""
    if not manifest_valid:
        diagnostics.append(f"Manifest 异常: {manifest_error or '未知错误'}")
    if not report.get("enabled", False):
        diagnostics.append("插件已禁用，不会加载。")
    if report.get("missing_env"):
        diagnostics.append(f"缺失环境变量: {_list_or_none(report['missing_env'])}")
    if manifest_valid and not report.get("entrypoint_importable", True):
        error = report.get("entrypoint_error") or "未知错误"
        diagnostics.append(f"入口不可导入: {error}")
    if manifest_valid and report.get("error"):
        diagnostics.append(f"加载错误: {report['error']}")
    if report.get("status") == "ERROR" and not report.get("error") and report.get("entrypoint_error"):
        diagnostics.append(f"加载错误: {report['entrypoint_error']}")
    if report.get("status") == "DEFERRED":
        reason = report.get("deferred_reason") or "平台/MCP 等触发时才会加载"
        diagnostics.append(f"延迟加载，当前未 import；{reason}。")
    for hint in report.get("diagnostic_hints") or []:
        item = f"建议: {hint}"
        if item not in diagnostics:
            diagnostics.append(item)
    if not diagnostics:
        diagnostics.append("当前无明显问题。")
    return diagnostics


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


def _agent_run_to_dict(run) -> dict[str, Any]:
    if is_dataclass(run):
        return asdict(run)
    return dict(run)


def _json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _exit_error(message: str) -> None:
    typer.secho(f"错误: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def run() -> None:
    app()
