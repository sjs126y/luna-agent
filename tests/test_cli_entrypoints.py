"""CLI entrypoint acceptance tests for real bootstrap paths."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from luna_agent.agent.agent import init_agent
from luna_agent.cli import app
from luna_agent.llm.provider import ProviderProfile
from luna_agent.models.messages import NormalizedResponse


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
        session_key="",
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
            memory_session_key=session_key,
            system_prompt_template=system_prompt_template,
            enabled_toolsets=settings.enabled_toolsets,
        )
        return SimpleNamespace(agent=agent, provider=provider, transport=transport)

    monkeypatch.setattr(
        "luna_agent.agent.factory.create_agent_runtime",
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
    assert data["runtime"]["boot"]["ok"] is True
    assert data["runtime"]["boot_ok"] is True
    assert data["runtime"]["boot_failed_step"] == ""
    assert data["runtime"]["turns"]["stored"] == 0
    assert data["runtime"]["gateway_created"] is False
    assert data["gateway"] == {}
    assert data["config"]["files"]["config"]["exists"] is True
    assert data["memory"]["builtin_available"] is True
    assert any(plugin["key"] == "builtin/tools" for plugin in data["plugins"])


def test_doctor_section_json_returns_only_requested_section(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["doctor", "--json", "--section", "tools"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "total" in data
    assert "by_permission" in data
    assert "runtime" not in data


def test_doctor_default_summary_and_verbose_output(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)

    summary = runner.invoke(app, ["doctor"])
    verbose = runner.invoke(app, ["doctor", "--verbose"])

    assert summary.exit_code == 0, summary.output
    assert "Luna Agent doctor" in summary.output
    assert "更多:" in summary.output
    assert "doctor --verbose" in summary.output
    assert "Effective Config:" not in summary.output
    assert "MCP 服务器:" not in summary.output

    assert verbose.exit_code == 0, verbose.output
    assert "Luna Agent doctor --verbose" in verbose.output
    assert "Effective Config:" in verbose.output
    assert "MCP 服务器:" in verbose.output


def test_doctor_execution_section_reports_profile(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)

    text_result = runner.invoke(app, ["doctor", "--section", "execution"])
    json_result = runner.invoke(app, ["doctor", "--json", "--section", "execution"])

    assert text_result.exit_code == 0, text_result.output
    assert "Luna Agent doctor: execution" in text_result.output
    assert "label: Ask First" in text_result.output
    assert "filesystem profile: read-only" in text_result.output
    assert "approval policy: on-request" in text_result.output
    assert "external tool approval: cached" in text_result.output

    assert json_result.exit_code == 0, json_result.output
    data = json.loads(json_result.output)
    assert data["mode"] == "ask-first"
    assert data["profile"] == "read-only"
    assert data["approval_policy"] == "on-request"
    assert data["grant_ttl_seconds"] == 3600


def test_doctor_section_text_filters_output(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["doctor", "--section", "platforms"])

    assert result.exit_code == 0, result.output
    assert "Luna Agent doctor: platforms" in result.output
    assert "platforms/qq" in result.output
    assert "Memory:" not in result.output


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


def test_chat_default_entrypoint_starts_inline_tui(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)
    calls = []

    def fake_inline(*, session_name="default"):
        calls.append(session_name)

    monkeypatch.setattr("luna_agent.tui.app.run_inline_tui_sync", fake_inline)
    result = runner.invoke(app, ["chat", "--session", "work"])

    assert result.exit_code == 0, result.output
    assert calls == ["work"]


def test_serve_dry_run_bootstraps_without_starting_platforms(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["serve", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "启动检查通过" in result.output
    assert "Boot: 正常" in result.output
    assert "Gateway 已创建: 是" in result.output
    assert "Gateway 运行: 否" in result.output
    assert "平台配置:" in result.output
    assert "platforms/qq" in result.output
    assert "配置=否" in result.output


def test_serve_dry_run_json_reports_scriptable_summary(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["serve", "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["runtime"]["boot"]["ok"] is True
    assert data["runtime"]["boot_ok"] is True
    assert data["runtime"]["gateway_created"] is True
    assert data["runtime"]["gateway_running"] is False
    assert "platforms" in data
    assert "config" in data


def test_serve_check_platform_reports_disabled_platform_without_failing(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "  disabled: []",
            "  disabled:\n    - platforms/qq",
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["serve", "--check-platform", "qq"])

    assert result.exit_code == 0, result.output
    assert "平台检查: qq" in result.output
    assert "platforms/qq" in result.output
    assert "启用=否" in result.output


def test_serve_check_platform_fails_when_enabled_platform_is_incomplete(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "  enabled: []",
            "  enabled:\n    - platforms/qq",
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["serve", "--check-platform", "qq", "--json"])

    assert result.exit_code == 1, result.output
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["platforms"][0]["enabled"] is True
    assert data["platforms"][0]["missing_env"] == ["QQ_BOT_WS_URL"]


def test_serve_check_platform_rejects_unknown_platform(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["serve", "--check-platform", "unknown"])

    assert result.exit_code == 1, result.output
    assert "unknown platform" in result.output


def test_doctor_json_reports_runtime_failure_without_traceback(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)

    async def broken_runtime(settings):
        from luna_agent.runtime import BootReport

        boot_report = BootReport.bootstrap()
        boot_report.error("memory", "RuntimeError: broken bootstrap")
        exc = RuntimeError("broken bootstrap")
        boot_report.attach_to_exception(exc)
        raise exc

    monkeypatch.setattr("luna_agent.runtime.create_app_runtime", broken_runtime)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["runtime"]["initialized"] is False
    assert "RuntimeError: broken bootstrap" in data["runtime"]["error"]
    assert data["runtime"]["boot"]["ok"] is False
    assert data["runtime"]["boot_failed_step"] == "memory"
    assert "Traceback" not in result.output


def test_serve_dry_run_json_reports_runtime_failure_boot(tmp_path, monkeypatch):
    _init_local_project(tmp_path, monkeypatch)

    async def broken_runtime(settings):
        from luna_agent.runtime import BootReport

        boot_report = BootReport.bootstrap()
        boot_report.error("database", "RuntimeError: db broken")
        exc = RuntimeError("db broken")
        boot_report.attach_to_exception(exc)
        raise exc

    monkeypatch.setattr("luna_agent.runtime.create_app_runtime", broken_runtime)

    result = runner.invoke(app, ["serve", "--dry-run", "--json"])

    assert result.exit_code == 1, result.output
    data = json.loads(result.output)
    assert data["runtime"]["initialized"] is False
    assert data["runtime"]["boot"]["ok"] is False
    assert data["runtime"]["boot_failed_step"] == "database"
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
    assert data["runtime"]["boot"]["ok"] is False
    assert data["runtime"]["boot_failed_step"] == "settings"
    assert any("LLM_MAX_TOKENS" in error for error in data["config"]["errors"])
    assert "Traceback" not in result.output
