"""CLI entrypoint acceptance tests for real bootstrap paths."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from personal_agent.agent.agent import init_agent
from personal_agent.cli import app
from personal_agent.llm.provider import ProviderProfile
from personal_agent.models.messages import NormalizedResponse


runner = CliRunner()


class EchoTransport:
    async def call(self, messages, system_prompt="", tools=None, max_tokens=4096, stream=False):
        last = messages[-1]["content"][0]["text"]
        return NormalizedResponse(
            text=f"echo:{last}",
            usage={"input_tokens": 2, "output_tokens": 3},
        )


def _install_echo_agent(monkeypatch) -> None:
    async def fake_create_agent_runtime(
        settings,
        *,
        memory_manager=None,
        plugin_manager=None,
        system_prompt_template="",
    ):
        provider = ProviderProfile(
            name="echo",
            base_url="https://example.test",
            api_key="test",
            model="echo-model",
            max_tokens=128,
            context_window=1000,
        )
        transport = EchoTransport()
        agent = init_agent(
            transport,
            provider,
            memory_manager=memory_manager,
            max_iterations=settings.max_iterations,
            max_tool_calls_per_turn=settings.max_tool_calls_per_turn,
            memory_review_interval=0,
            system_prompt_template=system_prompt_template,
            enabled_toolsets=settings.enabled_toolsets,
        )
        return SimpleNamespace(agent=agent, provider=provider, transport=transport)

    monkeypatch.setattr(
        "personal_agent.agent.factory.create_agent_runtime",
        fake_create_agent_runtime,
    )


def _init_local_project(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--dir", ".", "--profile", "local", "--fix-dirs"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "config.yaml").exists()
    assert (tmp_path / ".env.example").exists()
    assert (tmp_path / "data").exists()
    assert (tmp_path / "plugins").exists()
    assert (tmp_path / "data" / "system").exists()


def test_init_then_doctor_json_uses_real_runtime_bootstrap(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["runtime"]["initialized"] is True
    assert data["runtime"]["db_open"] is True
    assert data["runtime"]["gateway_created"] is False
    assert data["gateway"] == {}
    assert data["config"]["files"]["config"]["exists"] is True
    assert data["memory"]["builtin_available"] is True
    assert any(plugin["key"] == "builtin/tools" for plugin in data["plugins"])


def test_chat_once_entrypoint_bootstraps_runtime_and_persists_history(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)
    _install_echo_agent(monkeypatch)

    result = runner.invoke(app, ["chat", "--once", "你好", "--session", "work"])

    assert result.exit_code == 0, result.output
    assert "echo:你好" in result.output
    db_path = tmp_path / "data" / "state.db"
    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages ORDER BY id"
        ).fetchall()
    assert rows == [("user", "你好"), ("assistant", "echo:你好")]


def test_chat_repl_entrypoint_handles_commands_and_closes_runtime(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)
    _install_echo_agent(monkeypatch)

    result = runner.invoke(app, ["chat"], input="你好\n/usage\n/session work\n\n")

    assert result.exit_code == 0, result.output
    assert "Personal Agent CLI" in result.output
    assert "echo:你好" in result.output
    assert "上下文窗口" in result.output
    assert "会话已切换: cli:work:local" in result.output
    assert "deepseek-chat" in result.output
    assert "› " in result.output


def test_serve_dry_run_bootstraps_without_starting_platforms(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["serve", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "启动检查通过" in result.output
    assert "Gateway 已创建: 是" in result.output
    assert "Gateway 运行: 否" in result.output
    assert "平台配置:" in result.output
    assert "platforms/qq" in result.output
    assert "配置=否" in result.output


def test_doctor_json_reports_runtime_failure_without_traceback(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)

    async def broken_runtime(settings):
        raise RuntimeError("broken bootstrap")

    monkeypatch.setattr("personal_agent.runtime.create_app_runtime", broken_runtime)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["runtime"]["initialized"] is False
    assert "RuntimeError: broken bootstrap" in data["runtime"]["error"]
    assert "Traceback" not in result.output


def test_doctor_json_reports_settings_failure_without_crashing(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text(
        "LLM_PROVIDER=deepseek\nLLM_API_KEY=test\nLLM_MAX_TOKENS=bad\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["runtime"]["initialized"] is False
    assert "Settings 初始化失败" in data["runtime"]["error"]
    assert any("LLM_MAX_TOKENS" in error for error in data["config"]["errors"])
    assert "Traceback" not in result.output
