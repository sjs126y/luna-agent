from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

from personal_agent.config import Settings
from personal_agent.hooks import HookEnvelope, HookEvent, HookScope
from personal_agent.plugins import PluginManager


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins"
FILTER_PATH = PLUGIN_DIR / "codex_bridge" / "stdio_filter.py"


def test_codex_bridge_filter_only_drops_experimental_events():
    spec = importlib.util.spec_from_file_location("codex_bridge_stdio_filter", FILTER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._is_codex_event(b'{"jsonrpc":"2.0","method":"codex/event"}\n')
    assert not module._is_codex_event(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
    assert not module._is_codex_event(b"not-json\n")


@pytest.mark.asyncio
async def test_codex_bridge_registers_mcp_and_enforces_session_policy(tmp_path, monkeypatch):
    executable = tmp_path / "codex"
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda command: str(executable))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_home = tmp_path / ".codex"
    source_home.mkdir()
    (source_home / "auth.json").write_text("{}", encoding="utf-8")
    runtime_home = workspace / "data" / "codex-bridge"
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        sandbox_roots=[workspace],
        plugins_dirs=[PLUGIN_DIR],
        plugins_enabled=["integrations/codex-bridge"],
        plugins_config={
            "integrations/codex-bridge": {
                "source_codex_home": str(source_home),
                "runtime_codex_home": str(runtime_home),
                "cwd": str(workspace),
            }
        },
    )
    manager = PluginManager(
        settings,
        plugin_dirs=[PLUGIN_DIR],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )

    manager.load_enabled()

    plugin = manager.queries.plugin_info("integrations/codex-bridge")
    assert plugin["status"] == "LOADED"
    assert plugin["registered"]["mcp_servers"] == 1
    assert plugin["registered"]["hooks"] == 1
    server = manager.get_mcp_servers()[0]
    assert server.name == "codex"
    assert server.command == sys.executable
    assert server.args[0].endswith("plugins/codex_bridge/stdio_filter.py")
    assert server.args[1:] == [str(executable), "mcp-server"]
    assert server.env["CODEX_HOME"] == str(runtime_home.resolve())
    assert (runtime_home / "auth.json").read_text(encoding="utf-8") == "{}"
    assert (runtime_home / "auth.json").stat().st_mode & 0o777 == 0o600

    outcome = await manager.hook_manager.dispatch(HookEnvelope(
        event_name=HookEvent.PRE_TOOL_USE,
        scope=HookScope.TURN,
        payload={
            "tool_name": "mcp__codex__codex",
            "tool_input": {
                "prompt": "Inspect this project",
                "cwd": "/tmp",
                "sandbox": "danger-full-access",
                "approval-policy": "on-request",
                "config": {"mcp_servers": {"unsafe": {}}},
            },
        },
    ))

    assert outcome.updated_input == {
        "prompt": "Inspect this project",
        "cwd": str(workspace.resolve()),
        "sandbox": "workspace-write",
        "approval-policy": "never",
        "config": {"mcp_servers": {}},
    }


def test_codex_bridge_rejects_cwd_outside_writable_roots(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/codex")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_home = tmp_path / ".codex"
    source_home.mkdir()
    (source_home / "auth.json").write_text("{}", encoding="utf-8")
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        sandbox_roots=[workspace],
        plugins_dirs=[PLUGIN_DIR],
        plugins_enabled=["integrations/codex-bridge"],
        plugins_config={
            "integrations/codex-bridge": {
                "source_codex_home": str(source_home),
                "runtime_codex_home": str(workspace / "codex-home"),
                "cwd": str(tmp_path / "outside"),
            }
        },
    )
    manager = PluginManager(
        settings,
        plugin_dirs=[PLUGIN_DIR],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )

    manager.load_enabled()

    report = manager.queries.plugin_info("integrations/codex-bridge")
    assert report["status"] == "ERROR"
    assert "cwd must be within sandbox.roots" in report["error"]
