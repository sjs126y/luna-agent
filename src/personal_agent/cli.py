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
from personal_agent.config_registry import effective_config_snapshot
from personal_agent.context_budget import build_context_budget
from personal_agent.cli_chat import run_cli_once_sync
from personal_agent.main import boot
from personal_agent.plugins.core.manager import PluginManager

app = typer.Typer(help="Personal Agent")
plugins_app = typer.Typer(help="Manage plugins")
tokens_app = typer.Typer(help="Estimate token/context usage")
agents_app = typer.Typer(help="Run controlled agent helpers")
memory_app = typer.Typer(help="Inspect and manage memory")
protocol_app = typer.Typer(help="Inspect frontend/backend protocol contracts")

app.add_typer(plugins_app, name="plugins")
app.add_typer(tokens_app, name="tokens")
app.add_typer(agents_app, name="agents")
app.add_typer(memory_app, name="memory")
app.add_typer(protocol_app, name="protocol")


_CONFIG_TEMPLATE_LOCAL = """# Personal Agent minimal configuration
agent:
  max_iterations: 30
  max_tool_calls_per_turn: 20

llm:
  context_window: 0

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
  embedding:
    model: BAAI/bge-small-zh-v1.5
    relevance_threshold: 0.3
    max_prefetch: 3
    chunk_size: 800

compression:
  threshold_ratio: 0.6
  tail_token_budget: 20000

gateway:
  platform_reconnect_delays:
    - 1
    - 2
    - 5
    - 10
    - 30
    - 60
  platform_pending_warning_threshold: 10
  platform_chat_locks_maxsize: 64
  platform_message_dedupe_max_size: 1024
  platform_send_max_retries: 2

execution:
  mode: standard
  policy:
    tool_permissions: {}

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
  file_max_write_bytes: 100000
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

llm:
  context_window: 0

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
  embedding:
    model: BAAI/bge-small-zh-v1.5
    relevance_threshold: 0.3
    max_prefetch: 3
    chunk_size: 800

compression:
  threshold_ratio: 0.6
  tail_token_budget: 20000

gateway:
  platform_reconnect_delays:
    - 1
    - 2
    - 5
    - 10
    - 30
    - 60
  platform_pending_warning_threshold: 10
  platform_chat_locks_maxsize: 64
  platform_message_dedupe_max_size: 1024
  platform_send_max_retries: 2

execution:
  mode: standard
  policy:
    tool_permissions: {}

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
  file_max_write_bytes: 100000
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

llm:
  context_window: 0

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
  embedding:
    model: BAAI/bge-small-zh-v1.5
    relevance_threshold: 0.3
    max_prefetch: 3
    chunk_size: 800

compression:
  threshold_ratio: 0.6
  tail_token_budget: 20000

gateway:
  platform_reconnect_delays:
    - 1
    - 2
    - 5
    - 10
    - 30
    - 60
  platform_pending_warning_threshold: 10
  platform_chat_locks_maxsize: 64
  platform_message_dedupe_max_size: 1024
  platform_send_max_retries: 2

execution:
  mode: standard
  policy:
    tool_permissions: {}

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
  file_max_write_bytes: 100000
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
    "qq": _platform_config_template("qq"),
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
LLM_CONTEXT_WINDOW=0

# Platforms
TELEGRAM_BOT_TOKEN=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
WEIXIN_TOKEN=
WEIXIN_ACCOUNT_ID=
WEIXIN_USER_ID=
WEIXIN_BASE_URL=https://ilinkai.weixin.qq.com

QQ_BOT_BASE_URL=
QQ_BOT_TOKEN=
QQ_BOT_WEBHOOK_SECRET=
"""

_ENV_EXAMPLE_TEMPLATE_BOT = """# LLM
LLM_PROVIDER=deepseek
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_API_MODE=auto
LLM_MAX_TOKENS=4096
LLM_CONTEXT_WINDOW=0

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

# QQ / OneBot HTTP
QQ_BOT_BASE_URL=
QQ_BOT_TOKEN=
QQ_BOT_WEBHOOK_SECRET=
"""

_ENV_EXAMPLE_TEMPLATE_TELEGRAM = """# LLM
LLM_PROVIDER=deepseek
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_API_MODE=auto
LLM_MAX_TOKENS=4096
LLM_CONTEXT_WINDOW=0

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
LLM_CONTEXT_WINDOW=0

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
LLM_CONTEXT_WINDOW=0

# WeChat
WEIXIN_TOKEN=
WEIXIN_ACCOUNT_ID=
WEIXIN_USER_ID=
WEIXIN_BASE_URL=https://ilinkai.weixin.qq.com
"""

_ENV_EXAMPLE_TEMPLATE_QQ = """# LLM
LLM_PROVIDER=deepseek
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_API_MODE=auto
LLM_MAX_TOKENS=4096
LLM_CONTEXT_WINDOW=0

# QQ / OneBot HTTP
QQ_BOT_BASE_URL=
QQ_BOT_TOKEN=
QQ_BOT_WEBHOOK_SECRET=
"""

_ENV_EXAMPLE_TEMPLATES = {
    "bot": _ENV_EXAMPLE_TEMPLATE_BOT,
    "telegram": _ENV_EXAMPLE_TEMPLATE_TELEGRAM,
    "feishu": _ENV_EXAMPLE_TEMPLATE_FEISHU,
    "wechat": _ENV_EXAMPLE_TEMPLATE_WECHAT,
    "qq": _ENV_EXAMPLE_TEMPLATE_QQ,
}


@app.command()
def chat(
    message: str = typer.Argument("", help="可选：只运行一轮消息后退出。"),
    once: str = typer.Option("", "--once", "-o", help="只运行一轮消息后退出。"),
    session: str = typer.Option("default", "--session", "-s", help="CLI 会话名。"),
    ui: str = typer.Option("", "--ui", help="渲染器: inline。classic UI 已移除；不传则读 config.yaml agent.ui。"),
) -> None:
    """Interactive multi-turn chat loop."""
    one_shot = once or message
    # --ui overrides; when unset, fall back to config.yaml agent.ui (default inline).
    if not ui:
        try:
            ui = getattr(Settings(), "agent_ui", "inline") or "inline"
        except Exception:
            ui = "inline"
    try:
        if one_shot:
            run_cli_once_sync(one_shot, session_name=session)
        elif ui == "inline":
            from personal_agent.tui.app import run_inline_tui_sync

            run_inline_tui_sync(session_name=session)
        else:
            _exit_error(f"不支持的 chat UI: {ui}。classic UI 已移除，请使用 inline。")
    except Exception as exc:
        _exit_error(f"CLI 运行失败: {exc}")


@app.command()
def serve(
    dry_run: bool = typer.Option(False, "--dry-run", help="只执行启动装配检查，不连接平台。"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON，仅用于 --dry-run 或 --check-platform。"),
    check_platform: str = typer.Option("", "--check-platform", help="只检查指定平台: telegram|feishu|wechat|qq|all。"),
) -> None:
    """Run the platform gateway service."""
    if check_platform:
        asyncio.run(_serve_check_platform(check_platform, json_output=json_output))
        return
    if dry_run:
        asyncio.run(_serve_dry_run(json_output=json_output))
        return
    if json_output:
        _exit_error("--json 只能与 --dry-run 或 --check-platform 一起使用。")
    asyncio.run(boot())


@app.command("wechat-login")
def wechat_login() -> None:
    """Run the WeChat QR login helper."""
    async def _run() -> None:
        settings = Settings()
        plugin_manager = PluginManager(settings)
        plugin_manager.discover()
        plugin_manager.load_plugin("platforms/wechat")
        result = await plugin_manager.invoke_hook("wechat_qr_login", settings=settings)
        if result is None:
            _exit_error("WeChat login plugin is unavailable.")

    asyncio.run(_run())


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json", help="输出 JSON。"),
    section: str = typer.Option("all", "--section", help="输出部分诊断: all|runtime|config|platforms|tools|plugins。"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="输出完整开发诊断。"),
) -> None:
    """Show system diagnostics."""
    section = _normalize_doctor_section(section)
    report = build_doctor_report()
    if json_output:
        typer.echo(json.dumps(_doctor_section_payload(report, section), indent=2, ensure_ascii=False))
    else:
        typer.echo(format_doctor_report(report, section=section, verbose=verbose))


@app.command("init")
def init_project(
    target_dir: Path = typer.Option(Path("."), "--dir", "-d", help="生成配置的目录。"),
    profile: str = typer.Option("local", "--profile", "-p", help="配置模板: local|server|bot|telegram|feishu|wechat|qq。"),
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
        configured_limit = int(getattr(settings, "llm_context_window", 0) or 0)
        effective_limit = (
            context_limit
            or (configured_limit if not model else 0)
            or _detect_context_window(effective_model)
        )
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


@memory_app.command("ingest")
def memory_ingest(path: Path) -> None:
    """Ingest a file into external memory."""
    async def _run() -> None:
        if not path.exists():
            _exit_error(f"file not found: {path}")
        from personal_agent.runtime import create_app_runtime

        settings = Settings()
        runtime = await create_app_runtime(settings)
        try:
            ext = await runtime.plugin_manager.invoke_hook(
                "create_external_memory_provider",
                settings=runtime.settings,
                data_dir=runtime.data_dir / "memory",
                force=True,
            )
            if ext is None:
                _exit_error("external embedding memory provider is unavailable.")
            count = await ext.ingest_file(str(path.resolve()))
            typer.echo(f"Ingested {path.name}: {count} chunks stored.")
        except ValueError as exc:
            _exit_error(str(exc))
        finally:
            await runtime.close()

    asyncio.run(_run())


@protocol_app.command("schema")
def protocol_schema(
    json_output: bool = typer.Option(True, "--json/--no-json", help="输出 JSON。当前协议契约以 JSON 为准。"),
) -> None:
    """Print the frontend-facing event protocol schema."""
    if not json_output:
        _exit_error("protocol schema 当前只支持 JSON 输出，请使用 --json。")
    from personal_agent.conversation.events import frontend_protocol_schema

    typer.echo(_json_dumps(frontend_protocol_schema()))


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


async def _serve_dry_run(*, json_output: bool = False) -> None:
    report = await _build_serve_dry_run_report()
    if json_output:
        typer.echo(_json_dumps(report))
    else:
        if report.get("ok"):
            typer.echo("启动检查通过。")
        else:
            typer.echo("启动检查未通过。")
        runtime = report.get("runtime", {})
        typer.echo(f"数据目录: {runtime.get('data_dir') or report.get('data_dir') or '-'}")
        typer.echo(f"Boot: {_format_boot_summary(runtime.get('boot', {}))}")
        typer.echo(f"插件数: {runtime.get('plugins', 0)}")
        typer.echo(f"Gateway 已创建: {_yes(runtime.get('gateway_created', False))}")
        typer.echo(f"Gateway 运行: {_yes(runtime.get('gateway_running', False))}")
        typer.echo("平台配置:")
        platforms = report.get("platforms", [])
        if platforms:
            for platform in platforms:
                typer.echo(_format_platform_diagnostic_line(platform, include_runtime=False))
        else:
            typer.echo("  - 无")
        if report.get("next_steps"):
            typer.echo("下一步:")
            for step in report["next_steps"]:
                typer.echo(f"  - {step}")
    if not report.get("ok", False):
        raise typer.Exit(1)


async def _serve_check_platform(platform: str, *, json_output: bool = False) -> None:
    platform = platform.strip().lower()
    report = await _build_serve_dry_run_report()
    platforms = report.get("platforms", [])
    if platform != "all":
        platforms = [
            item for item in platforms
            if item.get("name") == platform or item.get("key") == f"platforms/{platform}"
        ]
    check_report = {
        "ok": bool(report.get("ok", False)),
        "platform": platform,
        "platforms": platforms,
        "next_steps": _platform_next_steps(platforms),
    }
    if platform != "all" and not platforms:
        check_report["ok"] = False
        check_report["error"] = f"unknown platform: {platform}"
        check_report["next_steps"] = [f"可选平台: {_list_or_none(_known_platform_names(report))}"]

    for item in platforms:
        if item.get("enabled") and (item.get("missing_env") or item.get("check_fn_passed") is False):
            check_report["ok"] = False

    if json_output:
        typer.echo(_json_dumps(check_report))
    else:
        label = platform if platform != "all" else "all"
        typer.echo(f"平台检查: {label}")
        if check_report.get("error"):
            typer.echo(f"  错误: {check_report['error']}")
        for item in platforms:
            typer.echo(_format_platform_diagnostic_line(item, include_runtime=False))
        if check_report.get("next_steps"):
            typer.echo("下一步:")
            for step in check_report["next_steps"]:
                typer.echo(f"  - {step}")
    if not check_report.get("ok", False):
        raise typer.Exit(1)


async def _build_serve_dry_run_report() -> dict[str, Any]:
    from personal_agent.runtime import create_app_runtime

    runtime = None
    try:
        settings = Settings()
        runtime = await create_app_runtime(settings)
        runtime.create_gateway(system_prompt_template="")
        _load_enabled_platform_plugins(runtime.plugin_manager)
        health = runtime.health_snapshot()
        config_report = build_config_report(Path("."))
        plugins = [
            runtime.plugin_manager.doctor_plugin(plugin.key)
            for plugin in runtime.plugin_manager.list_plugins()
        ]
        platforms = _platform_diagnostics_from_reports(
            plugins,
            health.get("gateway", {}),
            config_report,
            settings=settings,
        )
        errors = list(config_report.get("errors", []))
        return {
            "ok": not errors,
            "data_dir": str(settings.agent_data_dir),
            "runtime": health,
            "gateway": health.get("gateway", {}),
            "platforms": platforms,
            "config": {
                "ok": config_report.get("ok", False),
                "errors": errors,
                "warnings": list(config_report.get("warnings", [])),
            },
            "next_steps": list(config_report.get("next_steps", [])) if errors else [],
        }
    except Exception as exc:
        boot = _boot_dict_from_exception(exc)
        runtime_error = {
            "initialized": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        if boot:
            runtime_error.update({
                "boot": boot,
                "boot_ok": bool(boot.get("ok", False)),
                "boot_failed_step": str(boot.get("failed_step") or ""),
            })
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "runtime": runtime_error,
            "gateway": {},
            "platforms": [],
            "config": build_config_report(Path(".")),
            "next_steps": ["先修复启动检查错误，再运行 personal-agent serve --dry-run。"],
        }
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
        boot = _boot_dict_from_exception(exc)
        runtime_data = {
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
        }
        if boot:
            runtime_data.update({
                "boot": boot,
                "boot_ok": bool(boot.get("ok", False)),
                "boot_failed_step": str(boot.get("failed_step") or ""),
            })
        return {
            "runtime": runtime_data,
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
    if settings is None:
        try:
            settings = Settings()
        except Exception as exc:
            return _settings_failure_doctor_report(exc)
    runtime_health = _runtime_health_report(settings)
    plugins = runtime_health.pop("_plugins", None)
    if plugins is None:
        manager = _plugin_manager(settings)
        manager.load_enabled()
        plugins = [manager.doctor_plugin(plugin.key) for plugin in manager.list_plugins()]

    from personal_agent.llm.token_counter import tokenizer_status
    from personal_agent.tools.registry import tool_registry

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

    config_report = build_config_report(Path("."))
    platform_plugins = _platform_diagnostics_from_reports(
        plugins,
        runtime_health["runtime"].get("gateway", {}),
        config_report,
        settings=settings,
    )

    return {
        "data_dir": str(settings.agent_data_dir),
        "log_level": settings.log_level,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "mcp_enabled": settings.mcp_enabled,
        "config": config_report,
        "effective_config": effective_config_snapshot(settings),
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
        "execution": settings.execution_policy.as_dict(),
        "tools": tool_registry.catalog_summary(settings.enabled_toolsets),
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


def _settings_failure_doctor_report(exc: Exception) -> dict[str, Any]:
    from personal_agent.runtime import BootReport

    boot_report = BootReport.bootstrap()
    boot_report.error("settings", f"{type(exc).__name__}: {exc}")
    config_report = build_config_report(Path("."))
    data_dir = _config_directory(config_report, "data_dir", "./data")
    sandbox_roots = [
        {"path": item["path"], "exists": item["exists"]}
        for item in config_report.get("directories", [])
        if item.get("kind") == "sandbox_root"
    ]
    from personal_agent.llm.token_counter import tokenizer_status

    return {
        "data_dir": data_dir,
        "log_level": "-",
        "llm_provider": config_report.get("env", {}).get("llm_provider") or "-",
        "llm_model": config_report.get("env", {}).get("llm_model") or "-",
        "mcp_enabled": False,
        "config": config_report,
        "effective_config": {"field_count": 0, "fields": [], "sections": {}},
        "runtime": {
            "initialized": False,
            "db_open": False,
            "mcp_running": False,
            "gateway_created": False,
            "gateway_running": False,
            "cached_agents": 0,
            "error": f"Settings 初始化失败: {type(exc).__name__}: {exc}",
            "boot": boot_report.as_dict(),
            "boot_ok": False,
            "boot_failed_step": "settings",
        },
        "memory": {
            "builtin_available": False,
            "external_available": False,
            "providers": {},
            "review": {},
        },
        "gateway": {},
        "mcp_runtime": {},
        "agents": {},
        "execution": {
            "mode": "standard",
            "description": "balanced daily-use mode",
            "permissions": {},
            "network": "deny",
            "isolation": "policy-only",
            "warnings": [],
            "overrides": {"tool_permissions": {}},
        },
        "tools": _empty_tool_summary(),
        "sandbox": {
            "roots": sandbox_roots,
            "blocked_count": 0,
            "bash_work_dir": _config_directory(config_report, "bash_work_dir", "./data"),
        },
        "mcp_servers": config_report.get("mcp_servers", []),
        "platforms": [],
        "plugins": [],
        "tokenizer": tokenizer_status(),
    }


def _config_directory(report: dict[str, Any], kind: str, default: str) -> str:
    for item in report.get("directories", []):
        if item.get("kind") == kind:
            return str(item.get("path") or default)
    return default


_DOCTOR_SECTIONS = {"all", "runtime", "config", "execution", "platforms", "tools", "plugins"}


def _normalize_doctor_section(section: str) -> str:
    section = (section or "all").strip().lower()
    if section not in _DOCTOR_SECTIONS:
        _exit_error(f"未知 doctor section: {section}，可选: {_list_or_none(sorted(_DOCTOR_SECTIONS))}")
    return section


def _doctor_section_payload(report: dict[str, Any], section: str) -> dict[str, Any]:
    if section == "all":
        return report
    if section == "runtime":
        return {
            "runtime": report.get("runtime", {}),
            "gateway": report.get("gateway", {}),
            "mcp_runtime": report.get("mcp_runtime", {}),
        }
    if section == "config":
        return report.get("config", {})
    if section == "execution":
        return report.get("execution", {})
    if section == "platforms":
        return {"platforms": report.get("platforms", [])}
    if section == "tools":
        return report.get("tools", {})
    if section == "plugins":
        return {"plugins": report.get("plugins", [])}
    return report


def _boot_dict_from_exception(exc: BaseException) -> dict[str, Any]:
    from personal_agent.runtime import boot_report_from_exception

    boot_report = boot_report_from_exception(exc)
    return boot_report.as_dict() if boot_report is not None else {}


def _format_boot_summary(boot: dict[str, Any]) -> str:
    if not isinstance(boot, dict) or not boot:
        return "-"
    summary = boot.get("summary") or {}
    state = "正常" if boot.get("ok") else f"失败({boot.get('failed_step') or '-'})"
    return (
        f"{state} "
        f"total={summary.get('total', 0)} "
        f"ok={summary.get('ok', 0)} "
        f"skipped={summary.get('skipped', 0)} "
        f"not_run={summary.get('not_run', 0)} "
        f"error={summary.get('error', 0)}"
    )


def _format_boot_step_lines(boot: dict[str, Any]) -> list[str]:
    if not isinstance(boot, dict) or not boot.get("steps"):
        return ["  - 无"]
    lines = []
    for step in boot.get("steps", []):
        name = step.get("name") or "-"
        status = step.get("status") or "-"
        duration = step.get("duration", 0.0)
        detail = step.get("detail") or ""
        error = step.get("error") or ""
        suffix = ""
        if detail:
            suffix += f" detail={detail}"
        if error:
            suffix += f" error={error}"
        lines.append(f"  - {name}: {status} {duration:.3f}s{suffix}")
    return lines


def _format_turn_summary(turns: dict[str, Any]) -> str:
    if not isinstance(turns, dict) or not turns:
        return "-"
    persisted = turns.get("persisted") if isinstance(turns.get("persisted"), dict) else {}
    persisted_suffix = ""
    if persisted:
        persisted_suffix = (
            f" persisted={persisted.get('stored', 0)}"
            f" persisted_last={persisted.get('last_status') or '-'}"
        )
    return (
        f"stored={turns.get('stored', 0)} "
        f"last={turns.get('last_status') or '-'} "
        f"duration={float(turns.get('last_duration') or 0.0):.3f}s "
        f"llm={turns.get('last_llm_calls', 0)} "
        f"tools={turns.get('last_tool_calls', 0)} "
        f"retries={turns.get('last_retries', 0)}"
        f"{persisted_suffix}"
    )


def _format_tool_truth_summary(tool_truth: dict[str, Any]) -> str:
    if not isinstance(tool_truth, dict) or not tool_truth:
        return "-"
    return (
        f"inspected={tool_truth.get('inspected', 0)} "
        f"with_tools={tool_truth.get('turns_with_tools', 0)} "
        f"mismatches={tool_truth.get('claim_mismatches', 0)} "
        f"denied={tool_truth.get('denied_tool_calls', 0)}"
    )


def _format_tool_runs_summary(tool_runs: dict[str, Any]) -> str:
    if not isinstance(tool_runs, dict) or not tool_runs:
        return "-"
    return (
        f"stored={tool_runs.get('inspected', 0)} "
        f"denied={tool_runs.get('denied', 0)} "
        f"failed={tool_runs.get('failed', 0)} "
        f"truncated={tool_runs.get('truncated', 0)}"
    )


def _format_llm_cache_summary(cache: dict[str, Any]) -> str:
    if not isinstance(cache, dict) or not cache:
        return "-"
    usage = cache.get("last_usage") or {}
    return (
        f"strategy={cache.get('strategy') or '-'} "
        f"usage={_yes(cache.get('supports_usage', False))} "
        f"hit={usage.get('cache_hit_tokens', 0)} "
        f"miss={usage.get('cache_miss_tokens', 0)} "
        f"rate={float(usage.get('cache_hit_rate') or 0.0):.2f}"
    )


def _format_turn_detail_lines(turns: dict[str, Any]) -> list[str]:
    if not isinstance(turns, dict) or not turns:
        return ["  stored: 0"]
    lines = [
        f"  stored: {turns.get('stored', 0)}",
        f"  last status: {turns.get('last_status') or '-'}",
        f"  last duration: {float(turns.get('last_duration') or 0.0):.3f}s",
        f"  last llm calls: {turns.get('last_llm_calls', 0)}",
        f"  last tool calls: {turns.get('last_tool_calls', 0)}",
        f"  last tokens: in={turns.get('last_input_tokens', 0)} out={turns.get('last_output_tokens', 0)}",
        f"  last retries: {turns.get('last_retries', 0)}",
        f"  last error: {turns.get('last_error') or '-'}",
    ]
    persisted = turns.get("persisted") if isinstance(turns.get("persisted"), dict) else {}
    if persisted:
        lines.extend([
            f"  persisted stored: {persisted.get('stored', 0)}",
            f"  persisted last id: {persisted.get('last_id', 0)}",
            f"  persisted last status: {persisted.get('last_status') or '-'}",
            f"  persisted last session: {persisted.get('last_session_key') or '-'}",
            f"  persisted last turn: {persisted.get('last_turn_id') or '-'}",
            f"  persisted last error: {persisted.get('last_error') or '-'}",
            (
                "  persisted last cache: "
                f"hit={persisted.get('last_cache_hit_tokens', 0)} "
                f"miss={persisted.get('last_cache_miss_tokens', 0)} "
                f"write={persisted.get('last_cache_write_tokens', 0)} "
                f"read={persisted.get('last_cache_read_tokens', 0)}"
            ),
        ])
    return lines


def _format_tool_truth_detail_lines(tool_truth: dict[str, Any]) -> list[str]:
    if not isinstance(tool_truth, dict) or not tool_truth:
        return ["  inspected: 0"]
    return [
        f"  stored: {tool_truth.get('stored', 0)}",
        f"  inspected: {tool_truth.get('inspected', 0)}",
        f"  turns with tools: {tool_truth.get('turns_with_tools', 0)}",
        f"  turns without tools: {tool_truth.get('turns_without_tools', 0)}",
        f"  claim mismatches: {tool_truth.get('claim_mismatches', 0)}",
        f"  denied tool calls: {tool_truth.get('denied_tool_calls', 0)}",
        f"  failed tool calls: {tool_truth.get('failed_tool_calls', 0)}",
        f"  tool counts: {_format_counts(tool_truth.get('tool_counts', {}))}",
        f"  warnings: {_format_counts(tool_truth.get('warning_counts', {}))}",
        f"  last warning: {tool_truth.get('last_warning') or '-'}",
    ]


def _format_tool_runs_detail_lines(tool_runs: dict[str, Any]) -> list[str]:
    if not isinstance(tool_runs, dict) or not tool_runs:
        return ["  stored: 0"]
    return [
        f"  stored: {tool_runs.get('inspected', 0)}",
        f"  denied: {tool_runs.get('denied', 0)}",
        f"  failed: {tool_runs.get('failed', 0)}",
        f"  timeouts: {tool_runs.get('timeouts', 0)}",
        f"  truncated: {tool_runs.get('truncated', 0)}",
        f"  tool counts: {_format_counts(tool_runs.get('tool_counts', {}))}",
        f"  status counts: {_format_counts(tool_runs.get('status_counts', {}))}",
        f"  category counts: {_format_counts(tool_runs.get('category_counts', {}))}",
    ]


def _format_llm_cache_detail_lines(cache: dict[str, Any]) -> list[str]:
    if not isinstance(cache, dict) or not cache:
        return ["  strategy: -"]
    usage = cache.get("last_usage") or {}
    diagnostics = cache.get("last_diagnostics") or {}
    return [
        f"  provider: {cache.get('provider') or '-'}",
        f"  model: {cache.get('model') or '-'}",
        f"  strategy: {cache.get('strategy') or '-'}",
        f"  supports usage: {_yes(cache.get('supports_usage', False))}",
        f"  cacheable blocks: {_list_or_none(cache.get('cacheable_blocks', []))}",
        f"  usage fields: {_format_counts(cache.get('usage_fields', {}))}",
        (
            "  last usage: "
            f"hit={usage.get('cache_hit_tokens', 0)} "
            f"miss={usage.get('cache_miss_tokens', 0)} "
            f"write={usage.get('cache_write_tokens', 0)} "
            f"read={usage.get('cache_read_tokens', 0)} "
            f"rate={float(usage.get('cache_hit_rate') or 0.0):.2f}"
        ),
        f"  system hash: {diagnostics.get('system_hash') or '-'}",
        f"  tools hash: {diagnostics.get('tools_hash') or '-'}",
        f"  stable prefix hash: {diagnostics.get('stable_prefix_hash') or '-'}",
        f"  dynamic context hash: {diagnostics.get('dynamic_context_hash') or '-'}",
        f"  stable blocks: {diagnostics.get('stable_block_count', 0)}",
        f"  dynamic blocks: {diagnostics.get('dynamic_block_count', 0)}",
        f"  current user present: {_yes(diagnostics.get('current_user_present', False))}",
        f"  error: {cache.get('error') or '-'}",
    ]


def _format_command_health_summary(commands: dict[str, Any]) -> str:
    if not isinstance(commands, dict) or not commands:
        return "-"
    return (
        f"registry=v{commands.get('registry_version', 0)} "
        f"core={commands.get('core_commands', 0)} "
        f"plugins={commands.get('plugin_commands', 0)} "
        f"arguments={commands.get('argument_specs', 0)} "
        f"providers={_list_or_none(commands.get('dynamic_providers', []))}"
    )


def _format_command_health_detail_lines(commands: dict[str, Any]) -> list[str]:
    if not isinstance(commands, dict) or not commands:
        return ["  registry: unavailable"]
    return [
        f"  registry version: {commands.get('registry_version', 0)}",
        f"  core commands: {commands.get('core_commands', 0)}",
        f"  plugin commands: {commands.get('plugin_commands', 0)}",
        f"  argument specs: {commands.get('argument_specs', 0)}",
        f"  dynamic providers: {_list_or_none(commands.get('dynamic_providers', []))}",
        f"  /tool-runs: {_yes(commands.get('has_tool_runs', False))}",
        f"  /mode set arguments: {_yes(commands.get('has_mode_arguments', False))}",
        f"  /allow arguments: {_yes(commands.get('has_allow_arguments', False))}",
    ]


def _format_query_health_summary(query: dict[str, Any]) -> str:
    if not isinstance(query, dict) or not query:
        return "-"
    return (
        f"conversation={_yes(query.get('conversation_query_service', False))} "
        f"tool_runs={_yes(query.get('tool_runs_query', False))}"
    )


def _format_query_health_detail_lines(query: dict[str, Any]) -> list[str]:
    if not isinstance(query, dict) or not query:
        return ["  conversation query service: 否"]
    return [
        f"  conversation query service: {_yes(query.get('conversation_query_service', False))}",
        f"  tool runs query: {_yes(query.get('tool_runs_query', False))}",
    ]


def _format_runtime_execution_summary(execution: dict[str, Any]) -> str:
    if not isinstance(execution, dict) or not execution:
        return "-"
    return (
        f"mode={execution.get('mode') or '-'} "
        f"label={execution.get('label') or '-'} "
        f"isolation={execution.get('isolation') or '-'}"
    )


def _format_runtime_execution_detail_lines(execution: dict[str, Any]) -> list[str]:
    if not isinstance(execution, dict) or not execution:
        return ["  mode: -"]
    return [
        f"  mode: {execution.get('mode') or '-'}",
        f"  label: {execution.get('label') or '-'}",
        f"  isolation: {execution.get('isolation') or '-'}",
        f"  network: {execution.get('network') or '-'}",
        f"  permissions: {_format_permissions(execution.get('permissions', {}))}",
    ]


def format_doctor_report(report: dict[str, Any], *, section: str = "all", verbose: bool = False) -> str:
    section = _normalize_doctor_section(section)
    if section != "all":
        return _format_doctor_section(report, section)
    if not verbose:
        return _format_doctor_summary_report(report)

    issues = _doctor_issues(report)
    plugin_summary = _plugin_status_summary(report["plugins"])
    runtime = report.get("runtime", {})
    memory = report.get("memory", {})
    gateway = report.get("gateway", {})
    tools = report.get("tools", {})
    config = report.get("config", {})
    tool_truth = runtime.get("tool_truth", {})
    tool_runs = runtime.get("tool_runs", {})
    llm_cache = runtime.get("llm_cache", {})
    commands = runtime.get("commands", {})
    query = runtime.get("query", {})
    execution_runtime = runtime.get("execution", {})
    mcp_runtime = report.get("mcp_runtime") or runtime.get("mcp") or {}
    lines = [
        "Lumora doctor --verbose",
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
        f"  Boot: {_format_boot_summary(runtime.get('boot', {}))}",
        f"  Turns: {_format_turn_summary(runtime.get('turns', {}))}",
        f"  LLM Cache: {_format_llm_cache_summary(llm_cache)}",
        f"  Tool Truth: {_format_tool_truth_summary(tool_truth)}",
        f"  Tool Runs: {_format_tool_runs_summary(tool_runs)}",
        f"  Commands: {_format_command_health_summary(commands)}",
        f"  Query: {_format_query_health_summary(query)}",
        f"  Execution: {_format_runtime_execution_summary(execution_runtime)}",
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
        f"  errors: {len(config.get('errors', []))}",
        f"  warnings: {len(config.get('warnings', []))}",
    ]
    lines.extend(_format_effective_config_lines(report.get("effective_config", {})))
    lines.extend([
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
        f"  reconnect delays: {_list_or_none(gateway.get('platform_reconnect_delays', []))}",
        f"  pending warning threshold: {gateway.get('platform_pending_warning_threshold', 0)}",
        f"  chat locks maxsize: {gateway.get('platform_chat_locks_maxsize', 0)}",
        f"  dedupe max size: {gateway.get('platform_message_dedupe_max_size', 0)}",
        f"  send max retries: {gateway.get('platform_send_max_retries', 0)}",
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
        "Execution:",
    ])
    lines.extend(_format_execution_lines(report.get("execution", {})))
    lines.extend(["", "Tools:"])
    lines.extend(_format_tool_summary_lines(tools))
    lines.extend(["", "Sandbox:"])
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
            lines.append(_format_platform_diagnostic_line(platform, include_runtime=True))
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


def _format_doctor_summary_report(report: dict[str, Any]) -> str:
    issues = _doctor_issues(report)
    runtime = report.get("runtime") or {}
    memory = report.get("memory") or {}
    gateway = report.get("gateway") or {}
    tools = report.get("tools") or {}
    config = report.get("config") or {}
    plugin_summary = _plugin_status_summary(report.get("plugins") or [])
    mcp_runtime = report.get("mcp_runtime") or runtime.get("mcp") or {}
    platforms = list(report.get("platforms") or [])

    lines = [
        "Lumora doctor",
        "",
        f"状态: {_doctor_summary_status(report, issues)}",
        f"模型: {report.get('llm_provider') or '-'} / {report.get('llm_model') or '-'}",
        f"运行时: {_doctor_runtime_status(runtime)}",
        f"配置: {_doctor_config_status(config)}",
        f"记忆: {_doctor_memory_summary(memory)}",
        f"MCP: {_doctor_mcp_summary(report, mcp_runtime)}",
        f"工具: {int(tools.get('available') or 0)} 可用 / {int(tools.get('total') or 0)} 总数",
        f"网关: {_doctor_gateway_summary(gateway)}",
        f"平台: {_doctor_platform_summary(platforms)}",
        (
            "插件: "
            f"{plugin_summary['loaded']} 已加载, "
            f"{plugin_summary['deferred']} 延迟, "
            f"{plugin_summary['error']} 错误"
        ),
        "",
        "需要注意:",
    ]
    attention = _doctor_summary_attention(issues)
    lines.extend(f"  - {item}" for item in attention)
    lines.extend([
        "",
        "下一步:",
        *[f"  - {item}" for item in _doctor_next_steps(report, issues)],
        "",
        "更多:",
        "  - uv run personal-agent doctor --verbose",
        "  - uv run personal-agent doctor --json",
    ])
    return "\n".join(lines)


def _doctor_summary_status(report: dict[str, Any], issues: list[str]) -> str:
    config = report.get("config") or {}
    runtime = report.get("runtime") or {}
    has_config_errors = bool(config.get("errors"))
    runtime_failed = runtime and not runtime.get("initialized", False)
    llm_key = (config.get("env") or {}).get("llm_api_key_set")
    if has_config_errors or runtime_failed or llm_key is False:
        return "不可用，需要处理"
    if issues:
        return "可用，有提示"
    return "正常"


def _doctor_runtime_status(runtime: dict[str, Any]) -> str:
    if not runtime:
        return "未初始化"
    if not runtime.get("initialized", False):
        return "失败"
    if runtime.get("boot") and not (runtime.get("boot") or {}).get("ok", False):
        return "boot 异常"
    return "已就绪"


def _doctor_config_status(config: dict[str, Any]) -> str:
    if not config:
        return "未知"
    errors = len(config.get("errors") or [])
    warnings = len(config.get("warnings") or [])
    llm_key = (config.get("env") or {}).get("llm_api_key_set")
    if errors:
        return f"{errors} 个错误"
    if llm_key is False:
        return "缺少 LLM_API_KEY"
    if warnings:
        return f"正常，{warnings} 个提示"
    return "正常"


def _doctor_memory_summary(memory: dict[str, Any]) -> str:
    builtin = memory.get("builtin_provider") or ("file" if memory.get("builtin_available") else "-")
    external = memory.get("external_provider") or ("embedding" if memory.get("external_available") else "none")
    return f"{builtin} + {external}" if external and external != "none" else str(builtin or "-")


def _doctor_mcp_summary(report: dict[str, Any], mcp_runtime: dict[str, Any]) -> str:
    if not report.get("mcp_enabled", False):
        return "未启用"
    connected = int(mcp_runtime.get("connected_count") or 0)
    if not connected:
        connected = sum(1 for item in mcp_runtime.get("servers", []) if item.get("connected"))
    disabled = sum(1 for item in report.get("mcp_servers", []) if not item.get("enabled", True))
    configured = len(report.get("mcp_servers", []) or mcp_runtime.get("servers", []) or [])
    parts = [f"{connected} 已连接"]
    if disabled:
        parts.append(f"{disabled} 禁用")
    elif configured and connected < configured:
        parts.append(f"{configured - connected} 未连接")
    return ", ".join(parts)


def _doctor_gateway_summary(gateway: dict[str, Any]) -> str:
    if not gateway or not gateway.get("started", False):
        return "未运行"
    running = int(gateway.get("running_agents") or 0)
    pending = int(gateway.get("pending_messages") or 0)
    if running or pending:
        return f"运行中, agents={running}, pending={pending}"
    return "运行中"


def _doctor_platform_summary(platforms: list[dict[str, Any]]) -> str:
    if not platforms:
        return "未配置"
    configured = sum(1 for item in platforms if item.get("enabled", True) and not item.get("missing_env"))
    missing = sum(1 for item in platforms if item.get("missing_env"))
    deferred = sum(1 for item in platforms if str(item.get("status") or "").upper() == "DEFERRED")
    parts = [f"{configured} 已配置"]
    if missing:
        parts.append(f"{missing} 缺少环境变量")
    if deferred:
        parts.append(f"{deferred} 延迟加载")
    return ", ".join(parts)


def _doctor_summary_attention(issues: list[str], *, limit: int = 5) -> list[str]:
    if not issues:
        return ["无"]
    visible = issues[:limit]
    remaining = len(issues) - len(visible)
    if remaining > 0:
        visible.append(f"还有 {remaining} 条，运行 doctor --verbose 查看详情")
    return visible


def _doctor_next_steps(report: dict[str, Any], issues: list[str]) -> list[str]:
    config = report.get("config") or {}
    if (config.get("env") or {}).get("llm_api_key_set") is False:
        return [
            "编辑 .env，填写 LLM_API_KEY",
            "uv run personal-agent doctor",
        ]
    if config.get("errors"):
        return [
            "uv run personal-agent init --check",
            "uv run personal-agent doctor --verbose",
        ]
    steps = ["uv run personal-agent chat"]
    platforms = list(report.get("platforms") or [])
    if any(item.get("enabled", True) and not item.get("missing_env") for item in platforms):
        steps.append("uv run personal-agent serve")
    if issues:
        steps.append("uv run personal-agent doctor --verbose")
    return steps


def _format_tool_summary_lines(tools: dict[str, Any]) -> list[str]:
    lines = [
        f"  total: {tools.get('total', 0)}",
        f"  available: {tools.get('available', 0)}",
        f"  unavailable: {tools.get('unavailable', 0)}",
        f"  core: {tools.get('core', 0)}",
        f"  destructive: {tools.get('destructive', 0)}",
        f"  by toolset: {_format_counts(tools.get('by_toolset', {}))}",
        f"  by permission: {_format_counts(tools.get('by_permission', {}))}",
    ]
    if "by_risk" in tools:
        lines.append(f"  by risk: {_format_counts(tools.get('by_risk', {}))}")
    lines.append(f"  high risk: {_list_or_none(tools.get('high_risk', []))}")
    for item in tools.get("unavailable_tools", []):
        lines.append(f"  unavailable: {item.get('name') or '-'} ({item.get('reason') or '-'})")
    return lines


def _format_execution_lines(execution: dict[str, Any]) -> list[str]:
    profile = execution.get("profile") or {}
    sandbox = profile.get("sandbox") or {}
    network = profile.get("network") or {}
    grants = profile.get("grants") or {}
    audit = profile.get("audit") or {}
    overrides = execution.get("overrides") or {}
    tool_overrides = overrides.get("tool_permissions") or {}
    lines = [
        f"  mode: {execution.get('mode', '-')}",
        f"  profile: {profile.get('label') or '-'}",
        f"  description: {execution.get('description') or profile.get('description') or '-'}",
        f"  isolation: {execution.get('isolation', '-')}",
        f"  network: {execution.get('network', '-')}",
        f"  effective permissions: {_format_permissions(execution.get('permissions', {}))}",
        f"  overrides: {_format_permissions(tool_overrides)}",
    ]
    if sandbox:
        lines.extend([
            f"  sandbox: {sandbox.get('kind', '-')}",
            f"  hard prechecks: {_enforced(sandbox.get('hard_prechecks_enforced'))}",
            f"  path roots: {_enforced(sandbox.get('path_roots_enforced'))}",
            f"  blocked patterns: {_enforced(sandbox.get('blocked_patterns_enforced'))}",
            f"  bash path restrict: {_enforced(sandbox.get('bash_path_restrict'))}",
            f"  file write limit: {_enforced(sandbox.get('file_write_limit_enforced'))}",
        ])
    if network:
        lines.extend([
            f"  network tools: {network.get('tool_permission', '-')}",
            f"  bash network: {network.get('bash_network', '-')}",
        ])
    if grants:
        lines.append(
            f"  grants: {grants.get('scope', '-')} scoped /allow "
            f"{_list_or_none(grants.get('categories', []))}"
        )
    if audit:
        lines.append(
            "  audit: "
            f"enabled={_yes(bool(audit.get('enabled', False)))} "
            f"decisions={_yes(bool(audit.get('decisions', False)))} "
            f"results={_yes(bool(audit.get('results', False)))}"
        )
    for warning in execution.get("warnings", []):
        lines.append(f"  warning: {warning}")
    return lines


def _format_effective_config_lines(effective_config: dict[str, Any]) -> list[str]:
    fields = {
        str(item.get("path")): item
        for item in effective_config.get("fields", [])
        if isinstance(item, dict)
    }
    paths = [
        "execution.mode",
        "execution.policy",
        "sandbox.roots",
        "sandbox.blocked",
        "sandbox.bash_work_dir",
        "sandbox.bash_allow_network",
        "sandbox.bash_restrict_paths",
        "sandbox.file_max_write_bytes",
        "sandbox.audit_enabled",
        "gateway.platform_send_max_retries",
        "gateway.platform_message_dedupe_max_size",
        "memory.external_provider",
        "plugins.enabled",
        "LLM_API_KEY",
    ]
    lines = ["", f"Effective Config: {effective_config.get('field_count', 0)} fields"]
    for path in paths:
        item = fields.get(path)
        if not item:
            continue
        lines.append(f"  {path}: {_format_config_value(item.get('value'))}")
    return lines


def _format_effective_config_section(effective_config: dict[str, Any]) -> list[str]:
    sections = effective_config.get("sections") or {}
    if not sections:
        return []
    lines = [f"Effective Config: {effective_config.get('field_count', 0)} fields"]
    for section, fields in sorted(sections.items()):
        lines.append(f"  {section}:")
        for item in fields:
            if not isinstance(item, dict):
                continue
            path = item.get("path") or "-"
            value = _format_config_value(item.get("value"))
            source = item.get("source") or "-"
            lines.append(f"    {path}: {value} ({source})")
    return lines


def _format_config_value(value: Any) -> str:
    if isinstance(value, bool):
        return _yes(value)
    if isinstance(value, str):
        return value if value else "无"
    if value is None:
        return "无"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return _list_or_none(value)
    if isinstance(value, dict):
        if not value:
            return "无"
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _format_doctor_section(report: dict[str, Any], section: str) -> str:
    if section == "config":
        lines = [format_config_report(report.get("config", {}))]
        details = _format_effective_config_section(report.get("effective_config", {}))
        if details:
            lines.extend(["", *details])
        return "\n".join(lines)
    if section == "plugins":
        return format_plugin_list(report.get("plugins", []))
    lines = [f"Lumora doctor: {section}"]
    if section == "runtime":
        runtime = report.get("runtime", {})
        gateway = report.get("gateway", {})
        lines.extend([
            f"  初始化: {_yes(runtime.get('initialized', False))}",
            f"  Boot: {_format_boot_summary(runtime.get('boot', {}))}",
            f"  Turns: {_format_turn_summary(runtime.get('turns', {}))}",
            f"  DB 打开: {_yes(runtime.get('db_open', False))}",
            f"  MCP 运行: {_yes(runtime.get('mcp_running', False))}",
            f"  Gateway 已创建: {_yes(runtime.get('gateway_created', False))}",
            f"  Gateway 运行: {_yes(runtime.get('gateway_running', False))}",
            f"  adapters: {gateway.get('adapter_count', 0)}",
            f"  pending messages: {gateway.get('pending_messages', 0)}",
            f"  runtime 错误: {runtime.get('error') or '-'}",
        ])
        lines.extend(["", "Boot steps:"])
        lines.extend(_format_boot_step_lines(runtime.get("boot", {})))
        lines.extend(["", "Turns:"])
        lines.extend(_format_turn_detail_lines(runtime.get("turns", {})))
        lines.extend(["", "LLM Cache:"])
        lines.extend(_format_llm_cache_detail_lines(runtime.get("llm_cache", {})))
        lines.extend(["", "Tool Truth:"])
        lines.extend(_format_tool_truth_detail_lines(runtime.get("tool_truth", {})))
        lines.extend(["", "Tool Runs:"])
        lines.extend(_format_tool_runs_detail_lines(runtime.get("tool_runs", {})))
        lines.extend(["", "Commands:"])
        lines.extend(_format_command_health_detail_lines(runtime.get("commands", {})))
        lines.extend(["", "Query:"])
        lines.extend(_format_query_health_detail_lines(runtime.get("query", {})))
        lines.extend(["", "Execution:"])
        lines.extend(_format_runtime_execution_detail_lines(runtime.get("execution", {})))
    elif section == "platforms":
        platforms = report.get("platforms", [])
        if platforms:
            lines.extend(_format_platform_diagnostic_line(item, include_runtime=True) for item in platforms)
        else:
            lines.append("  - 无")
    elif section == "tools":
        tools = report.get("tools", {})
        lines.extend(_format_tool_summary_lines(tools))
    elif section == "execution":
        lines.extend(_format_execution_lines(report.get("execution", {})))
    return "\n".join(lines)


def format_config_report(report: dict[str, Any]) -> str:
    files = report.get("files", {})
    env = report.get("env", {})
    registry_fields = report.get("registry_fields", {})
    registry_schema = report.get("registry_schema", {})
    registry_coverage = report.get("registry_coverage", {})
    registry_source_counts = report.get("registry_source_counts", {})
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
        "配置字段:",
        f"  schema version: {registry_schema.get('version', '-')}",
        f"  known fields: {registry_fields.get('field_count', 0)}",
        f"  schema fields: {registry_schema.get('field_count', 0)}",
        f"  config.yaml fields: {registry_coverage.get('config_yaml_field_count', registry_fields.get('config_yaml_field_count', 0))}",
        f"  sections: {_list_or_none((registry_fields.get('sections') or {}).keys())}",
        f"  config sections: {_list_or_none(registry_coverage.get('config_yaml_sections', []))}",
        f"  present sections: {_list_or_none(registry_coverage.get('present_config_sections', []))}",
        f"  source counts: {_format_counts(registry_source_counts)}",
        "",
        "平台:",
    ]
    for platform in env.get("platforms", []):
        lines.append(_format_platform_diagnostic_line(platform, include_runtime=False))
    if not env.get("platforms"):
        lines.append("  - 无")
    lines.extend([
        "",
        "目录:",
    ])
    for item in report.get("directories", []):
        lines.append(
            f"  - {item['kind']}: {item['path']} [{_status(item['exists'])}]"
        )
    if report.get("unknown_keys"):
        lines.extend(["", f"未知配置: {_list_or_none(report['unknown_keys'])}"])
    if report.get("unknown_nested_keys"):
        lines.extend(["", f"未知嵌套配置: {_list_or_none(report['unknown_nested_keys'])}"])
    if report.get("deprecated_keys"):
        lines.append("")
        lines.append("已废弃配置:")
        for item in report["deprecated_keys"]:
            lines.append(f"  - {item['key']}: {item['message']}")
    if report.get("registry_validation_errors"):
        lines.extend(["", "Registry 校验错误:"])
        lines.extend(f"  - {error}" for error in report["registry_validation_errors"])
    if report.get("registry_validation_warnings"):
        lines.extend(["", "Registry 校验警告:"])
        lines.extend(f"  - {warning}" for warning in report["registry_validation_warnings"])
    if report.get("registry_loader_errors"):
        lines.extend(["", "Registry Loader 错误:"])
        lines.extend(f"  - {error}" for error in report["registry_loader_errors"])
    if report.get("registry_loader_warnings"):
        lines.extend(["", "Registry Loader 警告:"])
        lines.extend(f"  - {warning}" for warning in report["registry_loader_warnings"])
    if report.get("migration_hints"):
        lines.extend(["", "迁移建议:"])
        lines.extend(f"  - {hint}" for hint in report["migration_hints"])
    if report.get("errors"):
        lines.extend(["", "错误:"])
        lines.extend(f"  - {error}" for error in report["errors"])
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
        f"Manifest 未知字段: {_list_or_none(report.get('manifest_unknown_fields', []))}",
        f"入口: {report['entrypoint']} [{_entrypoint_status_text(report)}]",
        f"启用: {_yes(report['enabled'])}  默认启用: {_yes(report['enabled_by_default'])}  延迟加载: {_yes(report['deferred'])}",
        f"状态: {report['status']}",
        f"提供能力: {_list_or_none(report['provides'])}",
        f"需要环境变量: {_list_or_none(report['requires_env'])}",
        f"缺失环境变量: {_list_or_none(report['missing_env'])}",
        f"来源边界: 声明={report.get('declared_source') or report.get('source') or '-'} 实际={report.get('source') or '-'} 路径={report.get('source_boundary') or '-'}",
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
    for error in (report.get("config") or {}).get("errors", []):
        issues.append(f"配置错误: {error}")
    for warning in (report.get("config") or {}).get("warnings", []):
        issues.append(f"配置: {warning}")

    runtime = report.get("runtime", {})
    if runtime:
        if not runtime.get("initialized", False):
            issues.append(f"Runtime 初始化失败: {runtime.get('error') or '未知错误'}")
        elif not runtime.get("db_open", False):
            issues.append("Runtime DB 未打开。")
        boot = runtime.get("boot") or {}
        if boot and not boot.get("ok", False):
            issues.append(f"Runtime boot 失败: {boot.get('failed_step') or '未知阶段'}")

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


def _platform_diagnostics_from_reports(
    plugins: list[dict[str, Any]],
    gateway: dict[str, Any],
    config_report: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    config_platforms = _platform_config_index(config_report)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    for plugin in plugins:
        if plugin.get("kind") != "platform":
            continue
        key = str(plugin.get("key") or "")
        name = key.split("/")[-1] if key else str(plugin.get("name") or "")
        config_item = config_platforms.get(key) or config_platforms.get(name) or {}
        missing_env = list(plugin.get("missing_env") or config_item.get("missing_env") or [])
        requires_env = list(plugin.get("requires_env") or config_item.get("required_env") or [])
        enabled = bool(plugin.get("enabled", False))
        hints = list(plugin.get("diagnostic_hints") or [])
        if not hints and config_item.get("hint"):
            hints.append(str(config_item["hint"]))
        check_fn_passed, check_fn_error = _platform_check_status(name, settings)
        result.append({
            "key": key or config_item.get("key") or name,
            "name": name,
            "status": plugin.get("status") or config_item.get("status") or "-",
            "enabled": enabled,
            "configured": enabled and not missing_env,
            "requires_env": requires_env,
            "missing_env": missing_env,
            "env_set": list(config_item.get("env_set") or []),
            "hint": hints[0] if hints else "",
            "check_fn_passed": check_fn_passed,
            "check_fn_error": check_fn_error,
            "health": _platform_health_for_plugin(gateway, plugin),
            "capabilities": _platform_capabilities_for_name(name),
        })
        seen.add(key)
        seen.add(name)

    for platform in config_report.get("env", {}).get("platforms", []):
        key = str(platform.get("key") or f"platforms/{platform.get('name')}")
        name = str(platform.get("name") or key.split("/")[-1])
        if key in seen or name in seen:
            continue
        check_fn_passed, check_fn_error = _platform_check_status(name, settings)
        result.append({
            "key": key,
            "name": name,
            "status": platform.get("status") or "-",
            "enabled": bool(platform.get("enabled", False)),
            "configured": bool(platform.get("configured", False)),
            "requires_env": list(platform.get("required_env") or []),
            "missing_env": list(platform.get("missing_env") or []),
            "env_set": list(platform.get("env_set") or []),
            "hint": platform.get("hint") or "",
            "check_fn_passed": check_fn_passed,
            "check_fn_error": check_fn_error,
            "health": {},
            "capabilities": _platform_capabilities_for_name(name),
        })
    return sorted(result, key=lambda item: str(item.get("key") or item.get("name") or ""))


def _load_enabled_platform_plugins(manager: PluginManager) -> None:
    for plugin in manager.list_plugins():
        if plugin.enabled and plugin.manifest.kind == "platform":
            manager.load_plugin(plugin.key)


def _platform_check_status(name: str, settings: Settings | None) -> tuple[bool | None, str]:
    if settings is None or not name:
        return None, ""
    try:
        from personal_agent.platforms.core import platform_registry

        for entry in platform_registry.list():
            if entry.name != name:
                continue
            return bool(entry.check_fn(settings)), ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return None, "platform entry not registered"


def _platform_capabilities_for_name(name: str) -> dict[str, Any]:
    if not name:
        return {}
    try:
        from personal_agent.platforms.core import platform_registry

        for entry in platform_registry.list():
            if entry.name == name:
                return entry.capabilities.as_dict()
    except Exception:
        return {}
    return {}


def _known_platform_names(report: dict[str, Any]) -> list[str]:
    return sorted(
        str(item.get("name") or "").strip()
        for item in report.get("platforms", [])
        if item.get("name")
    )


def _platform_next_steps(platforms: list[dict[str, Any]]) -> list[str]:
    steps: list[str] = []
    for platform in platforms:
        key = platform.get("key") or platform.get("name") or "-"
        missing = platform.get("missing_env") or []
        if missing:
            steps.append(f"{key}: 在 .env 中填写 {', '.join(str(item) for item in missing)}。")
        elif platform.get("enabled") and platform.get("check_fn_passed") is False:
            reason = platform.get("check_fn_error") or "check_fn returned False"
            steps.append(f"{key}: 修复本地平台配置 ({reason})。")
        elif not platform.get("enabled"):
            steps.append(f"{key}: 如需启用，请在 plugins.enabled 添加 {key}。")
    return _dedupe_strings(steps)


def _dedupe_strings(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result


def _platform_config_index(config_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for platform in config_report.get("env", {}).get("platforms", []) or []:
        key = str(platform.get("key") or "")
        name = str(platform.get("name") or "")
        if key:
            result[key] = platform
        if name:
            result[name] = platform
    return result


def _format_platform_diagnostic_line(
    platform: dict[str, Any],
    *,
    include_runtime: bool,
) -> str:
    line = (
        f"  - {platform.get('key') or platform.get('name') or '-'}: "
        f"状态={platform.get('status') or '-'} "
        f"启用={_yes(bool(platform.get('enabled', False)))} "
        f"配置={_yes(bool(platform.get('configured', False)))} "
        f"需要={_list_or_none(platform.get('requires_env') or platform.get('required_env') or [])} "
        f"缺失={_list_or_none(platform.get('missing_env') or [])}"
    )
    if platform.get("check_fn_passed") is not None:
        line += f" check_fn={_yes(bool(platform.get('check_fn_passed')))}"
    if platform.get("check_fn_error"):
        line += f" check_error={platform.get('check_fn_error')}"
    hint = str(platform.get("hint") or "")
    if hint:
        line += f" 提示={hint}"
    if include_runtime:
        health = platform.get("health") or {}
        connected = _yes(bool(health.get("connected", False))) if health else "-"
        error = (
            health.get("last_connect_error")
            or health.get("last_send_error")
            or health.get("last_error")
            or "-"
        )
        line += (
            f" runtime={health.get('status') or '-'}"
            f" connected={connected}"
            f" attempts={health.get('attempts', 0)}"
            f" pending={health.get('pending_messages', 0)}"
            f" next_retry={health.get('next_retry_at') or '-'}"
            f" error={error}"
        )
    capabilities = platform.get("capabilities") or (platform.get("health") or {}).get("capabilities") or {}
    capability_text = _format_platform_capabilities(capabilities)
    if capability_text:
        line += f" capabilities={capability_text}"
    return line


def _format_platform_capabilities(capabilities: dict[str, Any]) -> str:
    if not isinstance(capabilities, dict) or not capabilities:
        return ""
    labels = [
        ("text", "text"),
        ("markdown", "markdown"),
        ("rich_text", "rich"),
        ("image_send", "image"),
        ("file_send", "file"),
        ("audio_send", "audio"),
        ("video_send", "video"),
        ("mention", "mention"),
        ("reply", "reply"),
        ("typing", "typing"),
        ("attachments_in", "attachments-in"),
    ]
    enabled = [label for key, label in labels if bool(capabilities.get(key))]
    max_text_length = int(capabilities.get("max_text_length") or 0)
    if max_text_length > 0:
        enabled.append(f"max={max_text_length}")
    return ",".join(enabled)


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
    for warning in report.get("manifest_warnings") or []:
        diagnostics.append(f"Manifest 警告: {warning}")
    for warning in report.get("boundary_warnings") or []:
        diagnostics.append(f"边界警告: {warning}")
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


def _enforced(value) -> str:
    return "enforced" if bool(value) else "not enforced"


def _entrypoint_status_text(report: dict[str, Any]) -> str:
    if not report.get("entrypoint_checked", True):
        return "未检查"
    return _status(report.get("entrypoint_importable", True))


def _list_or_none(items) -> str:
    values = list(items or [])
    return ", ".join(str(item) for item in values) if values else "无"


def _format_counts(counts) -> str:
    if not isinstance(counts, dict) or not counts:
        return "无"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _empty_tool_summary() -> dict[str, Any]:
    return {
        "total": 0,
        "available": 0,
        "unavailable": 0,
        "core": 0,
        "parallel_safe": 0,
        "destructive": 0,
        "by_toolset": {},
        "by_permission": {},
        "by_risk": {},
        "by_tag": {},
        "high_risk": [],
        "unavailable_tools": [],
        "items": [],
    }


def _format_permissions(permissions) -> str:
    if not isinstance(permissions, dict) or not permissions:
        return "无"
    keys = ["default", "read", "search", "write", "bash", "background", "network", "destructive"]
    parts = [f"{key}={permissions[key]}" for key in keys if key in permissions]
    if not parts:
        parts = [f"{key}={value}" for key, value in sorted(permissions.items())]
    return ", ".join(parts)


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
