"""Typer CLI for Personal Agent."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Optional

import typer

from personal_agent.config import Settings
from personal_agent.context_budget import estimate_context_budget
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
def doctor() -> None:
    """Show system diagnostics."""
    settings = Settings()
    manager = _plugin_manager(settings)
    manager.load_enabled()

    typer.echo("Personal Agent doctor")
    typer.echo(f"data_dir: {settings.agent_data_dir}")
    typer.echo(f"log_level: {settings.log_level}")
    typer.echo(f"llm_provider: {settings.llm_provider}")
    typer.echo(f"mcp_enabled: {settings.mcp_enabled}")
    typer.echo("")

    typer.echo("sandbox roots:")
    for root in settings.sandbox_roots:
        status = "ok" if Path(root).exists() else "missing"
        typer.echo(f"  {root} [{status}]")

    if settings.mcp_servers:
        typer.echo("")
        typer.echo("mcp servers:")
        for server in settings.mcp_servers:
            command = server.get("command", "")
            status = "ok" if command and shutil.which(command) else "missing"
            typer.echo(f"  {server.get('name', command or 'unknown')}: {command or '-'} [{status}]")

    typer.echo("")
    typer.echo("plugins:")
    for plugin in manager.list_plugins():
        typer.echo(
            f"  {plugin.key}: {plugin.status.value} enabled={plugin.enabled} "
            f"registrations={plugin.registration_counts()}"
        )


@plugins_app.command("list")
def plugins_list(load: bool = typer.Option(False, help="Load enabled eager plugins before listing.")) -> None:
    manager = _plugin_manager()
    if load:
        manager.load_enabled()
    for plugin in manager.list_plugins():
        counts = plugin.registration_counts()
        typer.echo(
            f"{plugin.key}\t{plugin.status.value}\tenabled={plugin.enabled}\t"
            f"tools={counts['tools']} skills={counts['skills']} workflows={counts['workflows']} "
            f"platforms={counts['platforms']} hooks={counts['hooks']} commands={counts['commands']}"
        )


@plugins_app.command("info")
def plugins_info(key: str) -> None:
    manager = _plugin_manager()
    plugin = manager._plugins[key]
    data = {
        "key": plugin.key,
        "name": plugin.manifest.name,
        "version": plugin.manifest.version,
        "description": plugin.manifest.description,
        "kind": plugin.manifest.kind,
        "entrypoint": plugin.manifest.entrypoint,
        "enabled": plugin.enabled,
        "status": plugin.status.value,
        "deferred": plugin.deferred,
        "registered": plugin.registration_counts(),
        "error": plugin.error,
    }
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False))


@plugins_app.command("enable")
def plugins_enable(key: str) -> None:
    manager = _plugin_manager()
    plugin = manager.enable_plugin(key)
    typer.echo(f"enabled {plugin.key}")


@plugins_app.command("disable")
def plugins_disable(key: str) -> None:
    manager = _plugin_manager()
    plugin = manager.disable_plugin(key)
    typer.echo(f"disabled {plugin.key}")


@plugins_app.command("doctor")
def plugins_doctor(key: str) -> None:
    manager = _plugin_manager()
    plugin = manager.load_plugin(key)
    typer.echo(json.dumps(manager.doctor_plugin(plugin.key), indent=2, ensure_ascii=False))


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
) -> None:
    messages: list[dict] = []
    if session_json is not None:
        messages = json.loads(session_json.read_text(encoding="utf-8"))
    budget = estimate_context_budget(
        messages=messages,
        model=model,
        context_limit=context_limit,
    )
    typer.echo(json.dumps(budget.as_dict(), indent=2, ensure_ascii=False))


@agents_app.command("run")
def agents_run(prompt: str) -> None:
    typer.echo(
        "agents run requires a configured runtime inside an active agent session; "
        "use the delegate_task tool from chat/serve."
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


def run() -> None:
    app()
