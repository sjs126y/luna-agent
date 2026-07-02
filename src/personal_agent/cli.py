"""Typer CLI for Personal Agent."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

import typer

from personal_agent.config import Settings
from personal_agent.config_diagnostics import build_config_report, ensure_config_dirs
from personal_agent.context_budget import build_context_budget
from personal_agent.cli_chat import run_cli_once_sync, run_cli_repl_sync
from personal_agent.main import boot
from personal_agent.plugins.manager import PluginManager

app = typer.Typer(help="Personal Agent")
plugins_app = typer.Typer(help="Manage plugins")
tokens_app = typer.Typer(help="Estimate token/context usage")
agents_app = typer.Typer(help="Run controlled agent helpers")
memory_app = typer.Typer(help="Inspect and manage memory")

app.add_typer(plugins_app, name="plugins")
app.add_typer(tokens_app, name="tokens")
app.add_typer(agents_app, name="agents")
app.add_typer(memory_app, name="memory")


_CONFIG_TEMPLATE_LOCAL = """# Personal Agent minimal configuration
agent:
  max_iterations: 30
  max_tool_calls_per_turn: 20

agents:
  max_concurrent_runs: 4
  max_tool_calls: 10
  max_tokens: 4096
  history_limit: 100

storage:
  data_dir: ./data
  log_level: INFO

plugins:
  dirs:
    - ./plugins
    - ./data/plugins
  enabled: []
  disabled: []

memory:
  provider: file
  external_provider: none
  review_interval: 10

compression:
  threshold_ratio: 0.6
  tail_token_budget: 20000

sandbox:
  roots:
    - ./data
  blocked:
    - "**/.env"
    - "**/.git/**"
    - "**/.ssh/**"
  bash_work_dir: ./data
  bash_restrict_paths: true
  bash_allow_network: false
  audit_enabled: true

mcp:
  enabled: false
  servers: []

session:
  expire_days: 30
  override: {}

auth:
  enabled: false
  admins: []
"""

_CONFIG_TEMPLATE_SERVER = """# Personal Agent server configuration
agent:
  max_iterations: 30
  max_tool_calls_per_turn: 20

agents:
  max_concurrent_runs: 4
  max_tool_calls: 10
  max_tokens: 4096
  history_limit: 100

storage:
  data_dir: ./data
  log_level: INFO

plugins:
  dirs:
    - ./plugins
    - ./data/plugins
  enabled: []
  disabled: []

memory:
  provider: file
  external_provider: embedding
  review_interval: 10

compression:
  threshold_ratio: 0.6
  tail_token_budget: 20000

sandbox:
  roots:
    - ./data
  blocked:
    - "**/.env"
    - "**/.git/**"
    - "**/.ssh/**"
  bash_work_dir: ./data
  bash_restrict_paths: true
  bash_allow_network: false
  audit_enabled: true

mcp:
  enabled: false
  servers: []

session:
  expire_days: 30
  override: {}

auth:
  enabled: false
  admins: []
"""

_CONFIG_TEMPLATE_BOT = """# Personal Agent bot configuration
agent:
  max_iterations: 30
  max_tool_calls_per_turn: 20

agents:
  max_concurrent_runs: 4
  max_tool_calls: 10
  max_tokens: 4096
  history_limit: 100

storage:
  data_dir: ./data
  log_level: INFO

plugins:
  dirs:
    - ./plugins
    - ./data/plugins
  enabled: []
  disabled: []

memory:
  provider: file
  external_provider: embedding
  review_interval: 10

compression:
  threshold_ratio: 0.6
  tail_token_budget: 20000

sandbox:
  roots:
    - ./data
  blocked:
    - "**/.env"
    - "**/.git/**"
    - "**/.ssh/**"
  bash_work_dir: ./data
  bash_restrict_paths: true
  bash_allow_network: false
  audit_enabled: true

mcp:
  enabled: false
  servers: []

session:
  expire_days: 30
  override: {}

auth:
  enabled: true
  admins: []
  allowed_users: []
"""


def _platform_config_template(platform: str) -> str:
    return (
        _CONFIG_TEMPLATE_BOT
        .replace("# Personal Agent bot configuration", f"# Personal Agent {platform} bot configuration")
        .replace("  enabled: []", f"  enabled:\n    - platforms/{platform}")
    )


_CONFIG_TEMPLATES = {
    "local": _CONFIG_TEMPLATE_LOCAL,
    "server": _CONFIG_TEMPLATE_SERVER,
    "bot": _CONFIG_TEMPLATE_BOT,
    "telegram": _platform_config_template("telegram"),
    "feishu": _platform_config_template("feishu"),
    "wechat": _platform_config_template("wechat"),
}

_PROFILE_LIST = ", ".join(sorted(_CONFIG_TEMPLATES))
_CONFIG_TEMPLATE = _CONFIG_TEMPLATE_LOCAL


_ENV_EXAMPLE_TEMPLATE = """# LLM
LLM_PROVIDER=deepseek
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_API_MODE=auto
LLM_MAX_TOKENS=4096

# Platforms
TELEGRAM_BOT_TOKEN=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
WEIXIN_TOKEN=
WEIXIN_ACCOUNT_ID=
WEIXIN_USER_ID=
WEIXIN_BASE_URL=https://ilinkai.weixin.qq.com
"""

_ENV_EXAMPLE_TEMPLATE_BOT = """# LLM
LLM_PROVIDER=deepseek
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_API_MODE=auto
LLM_MAX_TOKENS=4096

# Telegram
TELEGRAM_BOT_TOKEN=

# Feishu
FEISHU_APP_ID=
FEISHU_APP_SECRET=

# WeChat
WEIXIN_TOKEN=
WEIXIN_ACCOUNT_ID=
WEIXIN_USER_ID=
WEIXIN_BASE_URL=https://ilinkai.weixin.qq.com
"""

_ENV_EXAMPLE_TEMPLATE_TELEGRAM = """# LLM
LLM_PROVIDER=deepseek
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_API_MODE=auto
LLM_MAX_TOKENS=4096

# Telegram
TELEGRAM_BOT_TOKEN=
"""

_ENV_EXAMPLE_TEMPLATE_FEISHU = """# LLM
LLM_PROVIDER=deepseek
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_API_MODE=auto
LLM_MAX_TOKENS=4096

# Feishu
FEISHU_APP_ID=
FEISHU_APP_SECRET=
"""

_ENV_EXAMPLE_TEMPLATE_WECHAT = """# LLM
LLM_PROVIDER=deepseek
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_API_MODE=auto
LLM_MAX_TOKENS=4096

# WeChat
WEIXIN_TOKEN=
WEIXIN_ACCOUNT_ID=
WEIXIN_USER_ID=
WEIXIN_BASE_URL=https://ilinkai.weixin.qq.com
"""

_ENV_EXAMPLE_TEMPLATES = {
    "bot": _ENV_EXAMPLE_TEMPLATE_BOT,
    "telegram": _ENV_EXAMPLE_TEMPLATE_TELEGRAM,
    "feishu": _ENV_EXAMPLE_TEMPLATE_FEISHU,
    "wechat": _ENV_EXAMPLE_TEMPLATE_WECHAT,
}


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
def serve(
    dry_run: bool = typer.Option(False, "--dry-run", help="只执行启动装配检查，不连接平台。"),
) -> None:
    """Run the platform gateway service."""
    if dry_run:
        asyncio.run(_serve_dry_run())
        return
    asyncio.run(boot())


@app.command()
def doctor(json_output: bool = typer.Option(False, "--json", help="输出 JSON。")) -> None:
    """Show system diagnostics."""
    report = build_doctor_report()
    if json_output:
        typer.echo(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        typer.echo(format_doctor_report(report))


@app.command("init")
def init_project(
    target_dir: Path = typer.Option(Path("."), "--dir", "-d", help="生成配置的目录。"),
    profile: str = typer.Option("local", "--profile", "-p", help="配置模板: local|server|bot|telegram|feishu|wechat。"),
    force: bool = typer.Option(False, "--force", "-f", help="覆盖已存在的文件。"),
    check: bool = typer.Option(False, "--check", help="只检查当前目录配置，不写配置文件。"),
    fix_dirs: bool = typer.Option(False, "--fix-dirs", help="创建 data/plugins/system 等基础目录。"),
    copy_env: bool = typer.Option(False, "--copy-env", help="从 .env.example 生成占位 .env。"),
) -> None:
    """Generate a minimal config.yaml and .env.example."""
    profile = profile.lower().strip()
    if profile not in _CONFIG_TEMPLATES:
        _exit_error(f"未知 profile: {profile}，可选: {_PROFILE_LIST}")

    if fix_dirs:
        created = ensure_config_dirs(target_dir)
        if created:
            typer.echo("已创建目录:")
            for path in created:
                typer.echo(f"  - {path}")
        else:
            typer.echo("基础目录已存在。")

    if check:
        report = build_config_report(target_dir)
        typer.echo(format_config_report(report))
        if not report.get("ok", False):
            raise typer.Exit(1)
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    results = [
        _write_template(target_dir / "config.yaml", _CONFIG_TEMPLATES[profile], force=force),
        _write_template(target_dir / ".env.example", _env_example_template(profile), force=force),
    ]
    if copy_env:
        results.append(
            _write_template(target_dir / ".env", _env_example_template(profile), force=force)
        )
    typer.echo(f"初始化 Personal Agent 配置: {target_dir} ({profile})")
    for path, action in results:
        typer.echo(f"  - {action}: {path}")


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


@plugins_app.command("validate")
def plugins_validate(
    path: Path,
    no_load: bool = typer.Option(False, "--no-load", help="只校验 manifest 和入口导入，不执行 register()。"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON。"),
) -> None:
    """Validate a local plugin directory or manifest file."""
    try:
        manager = _plugin_validation_manager(path)
        report = manager.validate_plugin_path(path, load=not no_load)
    except Exception as exc:
        _exit_error(f"插件校验失败: {exc}")

    if json_output:
        typer.echo(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        typer.echo(format_plugin_validation_report(report, include_traceback=True))
    if not report["validation_ok"]:
        raise typer.Exit(1)


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
        list_active_agent_runs,
        list_agent_runs,
    )

    if json_output:
        typer.echo(_json_dumps({
            "runs": list_agent_runs(limit=limit),
            "active_runs": list_active_agent_runs(),
        }))
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


@memory_app.command("doctor")
def memory_doctor(json_output: bool = typer.Option(False, "--json", help="输出 JSON。")) -> None:
    """Show memory provider and review diagnostics."""
    async def _run() -> None:
        report = await _memory_report()
        if json_output:
            typer.echo(_json_dumps(report))
        else:
            typer.echo(format_memory_doctor(report))

    asyncio.run(_run())


@memory_app.command("list")
def memory_list(
    target: str = typer.Option("all", "--target", "-t", help="all|memory|user|external"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON。"),
) -> None:
    """List memory entries."""
    async def _run() -> None:
        entries = await _memory_entries(target=target)
        if json_output:
            typer.echo(_json_dumps(entries))
        else:
            typer.echo(format_memory_entries(entries))

    asyncio.run(_run())


@memory_app.command("search")
def memory_search(
    query: str,
    target: str = typer.Option("all", "--target", "-t", help="all|memory|user|external"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON。"),
) -> None:
    """Search memory entries."""
    async def _run() -> None:
        entries = await _memory_search_entries(query, target=target)
        if json_output:
            typer.echo(_json_dumps(entries))
        else:
            typer.echo(format_memory_entries(entries, title="记忆搜索结果"))

    asyncio.run(_run())


@memory_app.command("show")
def memory_show(
    identifier: str,
    target: str = typer.Option("all", "--target", "-t", help="all|memory|user|external"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON。"),
) -> None:
    """Show one memory entry by id or index."""
    async def _run() -> None:
        entry = await _memory_entry(identifier, target=target)
        if entry is None:
            _exit_error(f"未找到记忆: {identifier}")
        if json_output:
            typer.echo(_json_dumps(entry))
        else:
            typer.echo(format_memory_entry(entry))

    asyncio.run(_run())


@memory_app.command("delete")
def memory_delete(
    identifier: str,
    target: str = typer.Option("all", "--target", "-t", help="all|memory|user|external"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认。"),
) -> None:
    """Delete one memory entry by id or index."""
    if not yes and not typer.confirm(f"确认删除记忆 {identifier}?"):
        typer.echo("已取消。")
        return

    async def _run() -> None:
        deleted = await _memory_delete(identifier, target=target)
        if not deleted:
            _exit_error(f"未找到或无法删除记忆: {identifier}")
        typer.echo(f"已删除记忆: {identifier}")

    asyncio.run(_run())


def _plugin_manager(settings: Settings | None = None) -> PluginManager:
    settings = settings or Settings()
    manager = PluginManager(settings)
    manager.discover()
    return manager


def _plugin_validation_manager(path: Path, settings: Settings | None = None) -> PluginManager:
    settings = settings or Settings()
    plugin_root = path.parent if path.name in {"plugin.yaml", "plugin.yml", "plugin.json"} else path
    validation_state = Path(tempfile.gettempdir()) / "personal-agent-plugin-validate-state.json"
    manager = PluginManager(
        settings,
        plugin_dirs=[plugin_root],
        state_path=validation_state,
        include_builtin=False,
    )
    return manager


def _load_agent_run_store(settings: Settings | None = None) -> Settings:
    settings = settings or Settings()
    from personal_agent.plugins.builtin.tools.builtin.delegate import load_agent_runs

    load_agent_runs(settings.agent_data_dir / "agent_runs.jsonl")
    return settings


async def _with_app_runtime(callback):
    from personal_agent.runtime import create_app_runtime

    runtime = await create_app_runtime(Settings())
    try:
        return await callback(runtime)
    finally:
        await runtime.close()


async def _memory_report() -> dict[str, Any]:
    async def _collect(runtime):
        memory = await runtime.memory_manager.health_snapshot()
        memory.update({
            "provider": runtime.settings.memory_provider,
            "external_provider_config": runtime.settings.memory_external_provider,
            "review_service": type(runtime.memory_review_service).__name__,
            "review_enabled": bool(getattr(runtime.memory_review_service, "enabled", False)),
            "review": runtime.memory_review_service.health_snapshot(),
        })
        return memory

    return await _with_app_runtime(_collect)


async def _memory_entries(*, target: str) -> list[dict[str, Any]]:
    async def _collect(runtime):
        return await runtime.memory_manager.list_entries(target=target)

    return await _with_app_runtime(_collect)


async def _memory_search_entries(query: str, *, target: str) -> list[dict[str, Any]]:
    async def _collect(runtime):
        return await runtime.memory_manager.search_entries(query, target=target)

    return await _with_app_runtime(_collect)


async def _memory_entry(identifier: str, *, target: str) -> dict[str, Any] | None:
    async def _collect(runtime):
        return await runtime.memory_manager.get_entry(identifier, target=target)

    return await _with_app_runtime(_collect)


async def _memory_delete(identifier: str, *, target: str) -> bool:
    async def _collect(runtime):
        return await runtime.memory_manager.delete(identifier, target=target)

    return await _with_app_runtime(_collect)


async def _serve_dry_run() -> None:
    from personal_agent.runtime import create_app_runtime

    runtime = None
    try:
        settings = Settings()
        runtime = await create_app_runtime(settings)
        runtime.create_gateway(system_prompt_template="")
        health = runtime.health_snapshot()
        typer.echo("启动检查通过。")
        typer.echo(f"数据目录: {health.get('data_dir')}")
        typer.echo(f"插件数: {health.get('plugins', 0)}")
        typer.echo(f"Gateway 已创建: {_yes(health.get('gateway_created', False))}")
        typer.echo(f"Gateway 运行: {_yes(health.get('gateway_running', False))}")
    except Exception as exc:
        _exit_error(f"启动检查失败: {exc}")
    finally:
        if runtime is not None:
            await runtime.close()


def _write_template(path: Path, content: str, *, force: bool) -> tuple[Path, str]:
    existed = path.exists()
    if existed and not force:
        return path, "已跳过"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return path, "已覆盖" if existed else "已生成"


def _env_example_template(profile: str) -> str:
    return _ENV_EXAMPLE_TEMPLATES.get(profile, _ENV_EXAMPLE_TEMPLATE)


def _runtime_health_report(settings: Settings) -> dict[str, Any]:
    return _run_async_sync(_runtime_health_report_async(settings))


async def _runtime_health_report_async(settings: Settings) -> dict[str, Any]:
    from personal_agent.runtime import create_app_runtime

    runtime = None
    try:
        runtime = await create_app_runtime(settings)
        runtime_data = runtime.health_snapshot()
        runtime_data.update({
            "initialized": True,
            "error": "",
        })
        memory = await runtime.memory_manager.health_snapshot()
        memory.update({
            "provider": settings.memory_provider,
            "external_provider_config": settings.memory_external_provider,
            "review_service": type(runtime.memory_review_service).__name__,
            "review_enabled": bool(getattr(runtime.memory_review_service, "enabled", False)),
            "review": runtime.memory_review_service.health_snapshot(),
        })
        plugins = [
            runtime.plugin_manager.doctor_plugin(plugin.key)
            for plugin in runtime.plugin_manager.list_plugins()
        ]
        return {
            "runtime": runtime_data,
            "memory": memory,
            "_plugins": plugins,
        }
    except Exception as exc:
        return {
            "runtime": {
                "initialized": False,
                "error": f"{type(exc).__name__}: {exc}",
                "data_dir": str(settings.agent_data_dir),
                "db_open": False,
                "mcp_enabled": bool(settings.mcp_enabled),
                "mcp_running": False,
                "mcp": {
                    "enabled": bool(settings.mcp_enabled),
                    "running": False,
                    "configured_count": len(settings.mcp_servers),
                    "connected_count": 0,
                    "total_tools": 0,
                    "registered_tools": [],
                    "servers": [],
                },
                "gateway_created": False,
                "gateway_running": False,
                "gateway": {},
                "plugins": 0,
                "cached_agents": 0,
                "closed": True,
            },
            "memory": {
                "provider": settings.memory_provider,
                "external_provider_config": settings.memory_external_provider,
                "builtin_available": False,
                "builtin_provider": "",
                "external_provider": "",
                "external_available": False,
                "review_service": "",
                "review_enabled": False,
                "providers": {},
                "review": {},
                "last_errors": {},
            },
            "_plugins": None,
        }
    finally:
        if runtime is not None:
            await runtime.close()


def _run_async_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import threading

    result: dict[str, Any] = {}
    error: list[BaseException] = []

    def _run() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result["value"]


def build_doctor_report(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or Settings()
    runtime_health = _runtime_health_report(settings)
    plugins = runtime_health.pop("_plugins", None)
    if plugins is None:
        manager = _plugin_manager(settings)
        manager.load_enabled()
        plugins = [manager.doctor_plugin(plugin.key) for plugin in manager.list_plugins()]

    from personal_agent.llm.token_counter import tokenizer_status

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
            "health": _platform_health_for_plugin(runtime_health["runtime"].get("gateway", {}), plugin),
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
        "config": build_config_report(Path(".")),
        "runtime": runtime_health["runtime"],
        "memory": runtime_health["memory"],
        "gateway": runtime_health["runtime"].get("gateway", {}),
        "mcp_runtime": runtime_health["runtime"].get("mcp", {}),
        "agents": {
            "max_concurrent_runs": settings.agent_runtime_max_concurrent_runs,
            "max_tool_calls": settings.agent_runtime_max_tool_calls,
            "max_tokens": settings.agent_runtime_max_tokens,
            "history_limit": settings.agent_runtime_history_limit,
        },
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
    runtime = report.get("runtime", {})
    memory = report.get("memory", {})
    gateway = report.get("gateway", {})
    config = report.get("config", {})
    mcp_runtime = report.get("mcp_runtime") or runtime.get("mcp") or {}
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
        "Runtime:",
        f"  初始化: {_yes(runtime.get('initialized', False))}",
        f"  DB 打开: {_yes(runtime.get('db_open', False))}",
        f"  MCP 运行: {_yes(runtime.get('mcp_running', False))}",
        f"  Gateway 已创建: {_yes(runtime.get('gateway_created', False))}",
        f"  Gateway 运行: {_yes(runtime.get('gateway_running', False))}",
        f"  cached agents: {runtime.get('cached_agents', 0)}",
        f"  runtime 错误: {runtime.get('error') or '-'}",
        "",
        "Config:",
        f"  config.yaml: {_yes(config.get('files', {}).get('config', {}).get('exists', False))}",
        f"  .env: {_yes(config.get('files', {}).get('env', {}).get('exists', False))}",
        f"  LLM key: {_yes(config.get('env', {}).get('llm_api_key_set', False))}",
        f"  unknown keys: {_list_or_none(config.get('unknown_keys', []))}",
        f"  warnings: {len(config.get('warnings', []))}",
        "",
        "Gateway:",
        f"  started: {_yes(gateway.get('started', False))}",
        f"  adapters: {gateway.get('adapter_count', 0)}",
        f"  running agents: {gateway.get('running_agents', 0)}",
        f"  stop requested: {gateway.get('stop_requested_agents', 0)}",
        f"  longest running seconds: {gateway.get('longest_running_seconds', 0)}",
        f"  pending messages: {gateway.get('pending_messages', 0)}",
        f"  active adapter sessions: {gateway.get('active_adapter_sessions', 0)}",
        f"  cron enabled: {_yes(gateway.get('cron_enabled', False))}",
        "",
        "Memory:",
        f"  builtin provider: {memory.get('builtin_provider') or '-'}",
        f"  external provider: {memory.get('external_provider') or '-'}",
        f"  review service: {memory.get('review_service') or '-'}",
        f"  review enabled: {_yes(memory.get('review_enabled', False))}",
        f"  builtin entries: {memory.get('providers', {}).get('builtin', {}).get('entries', 0)}",
        f"  external entries: {memory.get('providers', {}).get('external', {}).get('entries', 0)}",
        f"  review active: {_yes(memory.get('review', {}).get('active', False))}",
        f"  review last error: {memory.get('review', {}).get('last_error') or '-'}",
        "",
        "Agents:",
        f"  max concurrent runs: {report.get('agents', {}).get('max_concurrent_runs', 0)}",
        f"  max tool calls: {report.get('agents', {}).get('max_tool_calls', 0)}",
        f"  max tokens: {report.get('agents', {}).get('max_tokens', 0)}",
        f"  history limit: {report.get('agents', {}).get('history_limit', 0)}",
        "",
        "Sandbox:",
    ]
    for root in report["sandbox"]["roots"]:
        lines.append(f"  - {root['path']} [{_status(root['exists'])}]")
    lines.append(f"  blocked 规则: {report['sandbox']['blocked_count']}")
    lines.append(f"  bash 工作目录: {report['sandbox']['bash_work_dir']}")

    lines.extend(["", "MCP 服务器:"])
    runtime_servers = {
        str(server.get("name", "")): server
        for server in mcp_runtime.get("servers", [])
        if server.get("name")
    }
    seen_mcp_servers: set[str] = set()
    if report["mcp_servers"]:
        for server in report["mcp_servers"]:
            state = "禁用" if not server["enabled"] else _status(server["command_found"])
            name = str(server["name"])
            seen_mcp_servers.add(name)
            runtime_server = runtime_servers.get(name, {})
            runtime_text = _format_mcp_runtime_summary(runtime_server)
            lines.append(
                f"  - {name}: {server['command'] or '-'} [{state}] {runtime_text}"
            )
            stderr = _format_mcp_stderr(runtime_server)
            if stderr:
                lines.append(f"    stderr: {stderr}")
    for name, runtime_server in runtime_servers.items():
        if name in seen_mcp_servers:
            continue
        runtime_text = _format_mcp_runtime_summary(runtime_server)
        command = runtime_server.get("command") or "-"
        lines.append(f"  - {name}: {command} [runtime] {runtime_text}")
        stderr = _format_mcp_stderr(runtime_server)
        if stderr:
            lines.append(f"    stderr: {stderr}")
    if not report["mcp_servers"] and not runtime_servers:
        lines.append("  - 无")

    lines.extend(["", "平台配置:"])
    if report["platforms"]:
        for platform in report["platforms"]:
            missing = _list_or_none(platform["missing_env"])
            health = platform.get("health") or {}
            connected = _yes(bool(health.get("connected", False))) if health else "-"
            error = (
                health.get("last_connect_error")
                or health.get("last_send_error")
                or health.get("last_error")
                or "-"
            )
            runtime_status = health.get("status") or "-"
            attempts = health.get("attempts", 0)
            next_retry = health.get("next_retry_at") or "-"
            pending = health.get("pending_messages", 0)
            lines.append(
                f"  - {platform['key']}: 状态={platform['status']} "
                f"启用={_yes(platform['enabled'])} 缺失环境变量={missing} "
                f"runtime={runtime_status} connected={connected} attempts={attempts} "
                f"pending={pending} next_retry={next_retry} error={error}"
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
    if config.get("next_steps"):
        lines.extend(["", "下一步:"])
        lines.extend(f"  - {step}" for step in config["next_steps"])
    if config.get("recommended_commands"):
        lines.extend(["", "推荐命令:"])
        lines.extend(f"  - {command}" for command in config["recommended_commands"])
    return "\n".join(lines)


def format_config_report(report: dict[str, Any]) -> str:
    files = report.get("files", {})
    env = report.get("env", {})
    lines = [
        "配置检查",
        f"目录: {report.get('base_dir') or '-'}",
        f"总体状态: {'通过' if report.get('ok') else '需要处理'}",
        "",
        "文件:",
        f"  config.yaml: {_status(files.get('config', {}).get('exists', False))} ({files.get('config', {}).get('path', '-')})",
        f"  .env: {_status(files.get('env', {}).get('exists', False))} ({files.get('env', {}).get('path', '-')})",
        f"  .env.example: {_status(files.get('env_example', {}).get('exists', False))} ({files.get('env_example', {}).get('path', '-')})",
        "",
        "LLM:",
        f"  provider: {env.get('llm_provider') or '-'}",
        f"  API key: {_yes(env.get('llm_api_key_set', False))}",
        f"  base URL: {_yes(env.get('llm_base_url_set', False))}",
        f"  model: {_yes(env.get('llm_model_set', False))}",
        f"  缺失环境变量: {_list_or_none(env.get('missing_llm_env', []))}",
        "",
        "目录:",
    ]
    for item in report.get("directories", []):
        lines.append(
            f"  - {item['kind']}: {item['path']} [{_status(item['exists'])}]"
        )
    if report.get("unknown_keys"):
        lines.extend(["", f"未知配置: {_list_or_none(report['unknown_keys'])}"])
    if report.get("deprecated_keys"):
        lines.append("")
        lines.append("已废弃配置:")
        for item in report["deprecated_keys"]:
            lines.append(f"  - {item['key']}: {item['message']}")
    if report.get("migration_hints"):
        lines.extend(["", "迁移建议:"])
        lines.extend(f"  - {hint}" for hint in report["migration_hints"])
    if report.get("warnings"):
        lines.extend(["", "警告:"])
        lines.extend(f"  - {warning}" for warning in report["warnings"])
    if report.get("next_steps"):
        lines.extend(["", "下一步:"])
        lines.extend(f"  - {step}" for step in report["next_steps"])
    if report.get("recommended_commands"):
        lines.extend(["", "推荐命令:"])
        lines.extend(f"  - {command}" for command in report["recommended_commands"])
    return "\n".join(lines)


def format_memory_doctor(report: dict[str, Any]) -> str:
    providers = report.get("providers", {})
    builtin = providers.get("builtin", {})
    external = providers.get("external", {})
    review = report.get("review", {})
    lines = [
        "Memory 诊断",
        f"builtin provider: {report.get('builtin_provider') or '-'} [{_status(bool(report.get('builtin_available')))}]",
        f"external provider: {report.get('external_provider') or '-'} [{_status(bool(report.get('external_available')))}]",
        f"配置 builtin: {report.get('provider') or '-'}",
        f"配置 external: {report.get('external_provider_config') or '-'}",
        "",
        "Providers:",
        _format_memory_provider("builtin", builtin),
        _format_memory_provider("external", external),
        "",
        "Review:",
        f"  service: {report.get('review_service') or '-'}",
        f"  enabled: {_yes(review.get('enabled', report.get('review_enabled', False)))}",
        f"  active: {_yes(review.get('active', False))}",
        f"  cancel requested: {_yes(review.get('cancel_requested', False))}",
        f"  spawn count: {review.get('spawn_count', 0)}",
        f"  saved count: {review.get('saved_count', 0)}",
        f"  last started: {review.get('last_started') or '-'}",
        f"  last finished: {review.get('last_finished') or '-'}",
        f"  last error: {review.get('last_error') or '-'}",
    ]
    errors = report.get("last_errors") or {}
    if errors:
        lines.extend(["", "最近错误:"])
        lines.extend(f"  - {name}: {error}" for name, error in errors.items())
    return "\n".join(lines)


def _format_memory_provider(name: str, data: dict[str, Any]) -> str:
    if not data:
        return f"  - {name}: 未配置"
    parts = [
        f"  - {name}: {data.get('provider') or '-'}",
        f"available={_yes(bool(data.get('available')))}",
        f"entries={data.get('entries', 0)}",
    ]
    if data.get("memory_entries") is not None:
        parts.append(f"memory={data.get('memory_entries', 0)}")
    if data.get("user_entries") is not None:
        parts.append(f"user={data.get('user_entries', 0)}")
    if data.get("model"):
        parts.append(f"model={data.get('model')}")
    if data.get("last_error"):
        parts.append(f"error={data.get('last_error')}")
    return " ".join(parts)


def _format_mcp_runtime_summary(server: dict[str, Any]) -> str:
    if not server:
        return "runtime=-"
    error = server.get("last_error") or server.get("last_call_error") or "-"
    return (
        f"runtime=connected:{_yes(bool(server.get('connected', False)))} "
        f"tools={server.get('tool_count', 0)} "
        f"error={_short_text(str(error), 120)}"
    )


def _format_mcp_stderr(server: dict[str, Any]) -> str:
    tail = list(server.get("stderr_tail") or [])
    if not tail:
        return ""
    return _short_text(" | ".join(str(item) for item in tail[-2:]), 160)


def format_memory_entries(entries: list[dict[str, Any]], *, title: str = "记忆列表") -> str:
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


def format_memory_entry(entry: dict[str, Any]) -> str:
    return "\n".join([
        f"记忆: {entry.get('id') or entry.get('index') or '-'}",
        f"provider: {entry.get('provider') or '-'}",
        f"target: {entry.get('target') or '-'}",
        f"created_at: {entry.get('created_at') or '-'}",
        f"path: {entry.get('path') or '-'}",
        "",
        str(entry.get("text", "")),
    ])


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
        f"Manifest 文件: {report.get('manifest_path') or '-'}",
        f"Manifest: {_status(report.get('manifest_valid', True))}",
        f"入口: {report['entrypoint']} [{_entrypoint_status_text(report)}]",
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


def format_plugin_validation_report(report: dict[str, Any], *, include_traceback: bool) -> str:
    lines = [
        "插件校验",
        f"路径: {report.get('validation_path') or '-'}",
        f"Manifest 文件: {report.get('validation_manifest') or '-'}",
        f"加载测试: {'已执行' if report.get('validation_load_requested') else '跳过'}",
        f"校验结果: {'通过' if report.get('validation_ok') else '失败'}",
        "",
        format_plugin_report(report, include_traceback=include_traceback),
    ]
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
    for warning in (report.get("config") or {}).get("warnings", []):
        issues.append(f"配置: {warning}")

    runtime = report.get("runtime", {})
    if runtime:
        if not runtime.get("initialized", False):
            issues.append(f"Runtime 初始化失败: {runtime.get('error') or '未知错误'}")
        elif not runtime.get("db_open", False):
            issues.append("Runtime DB 未打开。")

    memory = report.get("memory", {})
    if memory and not memory.get("builtin_available", False):
        issues.append("内置 memory provider 不可用。")
    for name, provider in (memory.get("providers") or {}).items():
        if provider and provider.get("last_error"):
            issues.append(f"Memory provider {name} 错误: {provider['last_error']}")
    if (memory.get("review") or {}).get("last_error"):
        issues.append(f"Memory review 错误: {memory['review']['last_error']}")

    for root in report["sandbox"]["roots"]:
        if not root["exists"]:
            issues.append(f"Sandbox root 不存在: {root['path']}")

    for server in report["mcp_servers"]:
        if server["enabled"] and not server["command_found"]:
            issues.append(f"MCP 服务器 {server['name']} 的命令不可用: {server['command'] or '-'}")

    mcp_runtime = report.get("mcp_runtime") or runtime.get("mcp") or {}
    for server in mcp_runtime.get("servers", []):
        name = server.get("name") or "-"
        if server.get("last_error"):
            issues.append(f"MCP 服务器 {name} 连接失败: {server['last_error']}")
        if server.get("last_call_error"):
            issues.append(f"MCP 服务器 {name} 最近调用失败: {server['last_call_error']}")

    for platform in (report.get("gateway") or {}).get("platforms", []):
        connect_error = platform.get("last_connect_error") or platform.get("last_error")
        if connect_error:
            issues.append(f"平台 {platform.get('name') or '-'} 连接失败: {connect_error}")
        if platform.get("last_send_error"):
            issues.append(f"平台 {platform.get('name') or '-'} 发送失败: {platform['last_send_error']}")

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


def _platform_health_for_plugin(gateway: dict[str, Any], plugin: dict[str, Any]) -> dict[str, Any]:
    name = str(plugin.get("key", "")).split("/")[-1]
    for item in gateway.get("platforms", []) or []:
        if item.get("name") == name:
            return dict(item)
    return {}


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


def _entrypoint_status_text(report: dict[str, Any]) -> str:
    if not report.get("entrypoint_checked", True):
        return "未检查"
    return _status(report.get("entrypoint_importable", True))


def _list_or_none(items) -> str:
    values = list(items or [])
    return ", ".join(str(item) for item in values) if values else "无"


def _short_text(text: str, max_chars: int) -> str:
    value = " ".join(str(text).split())
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 1)] + "…"


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
