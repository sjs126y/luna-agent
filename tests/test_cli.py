"""CLI diagnostics and token commands."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from personal_agent.agents.runtime import AgentRun
from personal_agent.cli import (
    app,
    build_doctor_report,
    format_doctor_report,
    format_plugin_list,
    format_plugin_report,
)


runner = CliRunner()


def test_tokens_estimate_command_outputs_number():
    result = runner.invoke(app, ["tokens", "estimate", "hello"])

    assert result.exit_code == 0
    assert result.output.strip().isdigit()


def test_tokens_session_command_outputs_chinese_budget():
    result = runner.invoke(app, ["tokens", "session", "--context-limit", "1000"])

    assert result.exit_code == 0
    assert "上下文预算估算" in result.output
    assert "已用:" in result.output


def test_plugins_list_json_command():
    result = runner.invoke(app, ["plugins", "list", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert any(plugin["key"] == "builtin/tools" for plugin in data)


def test_plugins_list_command_shows_summary_and_groups():
    result = runner.invoke(app, ["plugins", "list"])

    assert result.exit_code == 0
    assert "插件概览:" in result.output
    assert "内置插件:" in result.output
    assert "平台插件:" in result.output
    assert "builtin/tools" in result.output


def test_chat_positional_message_runs_once(monkeypatch):
    calls = []

    def fake_once(message, *, session_name="default"):
        calls.append((message, session_name))

    monkeypatch.setattr("personal_agent.cli.run_cli_once_sync", fake_once)
    result = runner.invoke(app, ["chat", "你好", "--session", "work"])

    assert result.exit_code == 0
    assert calls == [("你好", "work")]


def test_chat_once_option_runs_once(monkeypatch):
    calls = []

    def fake_once(message, *, session_name="default"):
        calls.append((message, session_name))

    monkeypatch.setattr("personal_agent.cli.run_cli_once_sync", fake_once)
    result = runner.invoke(app, ["chat", "--once", "你好"])

    assert result.exit_code == 0
    assert calls == [("你好", "default")]


def test_agents_list_show_clear_commands(monkeypatch):
    monkeypatch.setattr("personal_agent.cli._load_agent_run_store", lambda: None)
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.get_agent_run",
        lambda run_id: AgentRun(run_id=run_id, parent_turn_id="", status="completed"),
    )
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.format_agent_runs",
        lambda limit=None: f"runs:{limit}",
    )
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.format_agent_run",
        lambda run_id: f"run:{run_id}",
    )
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.clear_agent_runs",
        lambda: 3,
    )

    listed = runner.invoke(app, ["agents", "list", "--limit", "5"])
    shown = runner.invoke(app, ["agents", "show", "abc123"])
    cleared = runner.invoke(app, ["agents", "clear"])

    assert listed.exit_code == 0
    assert "runs:5" in listed.output
    assert shown.exit_code == 0
    assert "run:abc123" in shown.output
    assert cleared.exit_code == 0
    assert "已清理 3 条" in cleared.output


def test_agents_json_commands(monkeypatch):
    run = AgentRun(
        run_id="abc123",
        parent_turn_id="turn1",
        status="completed",
        role="reviewer",
        task="inspect",
        result="ok",
        usage={"input_tokens": 1, "output_tokens": 2},
    )
    monkeypatch.setattr("personal_agent.cli._load_agent_run_store", lambda: None)
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.list_agent_runs",
        lambda limit=None: [{
            "schema_version": 2,
            "run_id": "abc123",
            "status": "completed",
            "role": "reviewer",
            "task": "inspect",
        }],
    )
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.get_agent_run",
        lambda run_id: run if run_id == "abc123" else None,
    )

    listed = runner.invoke(app, ["agents", "list", "--json"])
    shown = runner.invoke(app, ["agents", "show", "abc123", "--json"])

    assert listed.exit_code == 0
    assert json.loads(listed.output)[0]["run_id"] == "abc123"
    assert shown.exit_code == 0
    data = json.loads(shown.output)
    assert data["schema_version"] == 2
    assert data["run_id"] == "abc123"
    assert data["result"] == "ok"


def test_agents_export_command(monkeypatch, tmp_path):
    run = AgentRun(
        run_id="abc123",
        parent_turn_id="turn1",
        status="completed",
        role="reviewer",
        task="inspect",
        result="ok",
    )
    output = tmp_path / "run.json"
    monkeypatch.setattr(
        "personal_agent.cli._load_agent_run_store",
        lambda: type("SettingsStub", (), {"agent_data_dir": tmp_path})(),
    )
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.get_agent_run",
        lambda run_id: run if run_id == "abc123" else None,
    )

    result = runner.invoke(app, ["agents", "export", "abc123", "--output", str(output)])

    assert result.exit_code == 0
    assert "已导出子 agent" in result.output
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["run_id"] == "abc123"
    assert data["schema_version"] == 2


def test_agents_show_missing_outputs_chinese_error(monkeypatch):
    monkeypatch.setattr("personal_agent.cli._load_agent_run_store", lambda: None)
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.get_agent_run",
        lambda run_id: None,
    )

    result = runner.invoke(app, ["agents", "show", "missing"])

    assert result.exit_code == 1
    assert "错误: 未找到子 agent 运行记录: missing" in result.stderr


def test_plugins_info_command_shows_registered_items():
    result = runner.invoke(app, ["plugins", "info", "builtin/skills", "--load"])

    assert result.exit_code == 0
    assert "插件: builtin/skills" in result.output
    assert "注册项:" in result.output
    assert "python-expert" in result.output


def test_plugins_doctor_json_command():
    result = runner.invoke(app, ["plugins", "doctor", "builtin/skills", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["key"] == "builtin/skills"
    assert data["entrypoint_importable"] is True
    assert "python-expert" in data["registered_items"]["skills"]


def test_global_doctor_report_contains_tokenizer_and_plugins():
    report = build_doctor_report()

    assert "tokenizer" in report
    assert "tiktoken_available" in report["tokenizer"]
    assert any(plugin["key"] == "builtin/tools" for plugin in report["plugins"])


def test_format_plugin_report_includes_traceback_when_requested():
    report = {
        "key": "user/demo",
        "name": "Demo",
        "version": "1.0.0",
        "description": "",
        "kind": "user",
        "source": "user",
        "entrypoint": "demo:register",
        "entrypoint_importable": False,
        "enabled": True,
        "enabled_by_default": False,
        "deferred": False,
        "status": "ERROR",
        "provides": [],
        "requires_env": [],
        "missing_env": [],
        "registered": {
            "tools": 0,
            "skills": 0,
            "workflows": 0,
            "platforms": 0,
            "mcp_servers": 0,
            "hooks": 0,
            "commands": 0,
            "middleware": 0,
        },
        "registered_items": {
            "tools": [],
            "skills": [],
            "workflows": [],
            "platforms": [],
            "mcp_servers": [],
            "hooks": [],
            "commands": [],
            "middleware": [],
        },
        "error": "boom",
        "entrypoint_error": "",
        "error_traceback": "Traceback demo",
    }

    text = format_plugin_report(report, include_traceback=True)
    assert "诊断:" in text
    assert "入口不可导入" in text
    assert "加载错误: boom" in text
    assert "错误: boom" in text
    assert "Traceback demo" in text


def test_format_plugin_list_summarizes_and_groups_reports():
    reports = [
        _plugin_report("builtin/tools", status="LOADED", source="builtin"),
        _plugin_report("platforms/telegram", status="DEFERRED", kind="platform"),
        _plugin_report(
            "user/demo",
            status="ERROR",
            source="user",
            error="boom",
            missing_env=["DEMO_TOKEN"],
        ),
    ]

    text = format_plugin_list(reports)

    assert "插件概览: 总数=3 已加载=1 延迟=1 禁用=0 错误=1" in text
    assert "内置插件:" in text
    assert "平台插件:" in text
    assert "用户插件:" in text
    assert "问题=缺失环境变量: DEMO_TOKEN；加载错误: boom" in text


def test_format_doctor_report_includes_summary_and_issues():
    report = {
        "data_dir": "data",
        "log_level": "INFO",
        "llm_provider": "deepseek",
        "llm_model": "deepseek-v4-flash",
        "mcp_enabled": True,
        "sandbox": {
            "roots": [{"path": "/missing", "exists": False}],
            "blocked_count": 1,
            "bash_work_dir": "data",
        },
        "mcp_servers": [{
            "name": "demo",
            "command": "missing-cmd",
            "enabled": True,
            "command_found": False,
        }],
        "platforms": [],
        "plugins": [_plugin_report("user/demo", status="ERROR", error="boom")],
        "tokenizer": {
            "tiktoken_available": True,
            "fallback_active": False,
            "default_encoding": "cl100k_base",
            "cached_encodings": {},
        },
    }

    text = format_doctor_report(report)

    assert "总体状态: 需要注意" in text
    assert "插件概览: 总数=1 已加载=0 延迟=0 禁用=0 错误=1" in text
    assert "需要注意:" in text
    assert "Sandbox root 不存在: /missing" in text
    assert "MCP 服务器 demo 的命令不可用: missing-cmd" in text
    assert "插件 user/demo: 加载错误: boom" in text


def _plugin_report(
    key: str,
    *,
    status: str = "DISCOVERED",
    kind: str = "user",
    source: str = "user",
    enabled: bool = True,
    deferred: bool = False,
    error: str = "",
    entrypoint_error: str = "",
    missing_env: list[str] | None = None,
) -> dict:
    return {
        "key": key,
        "name": key,
        "version": "1.0.0",
        "description": "",
        "kind": kind,
        "source": source,
        "entrypoint": "demo:register",
        "entrypoint_importable": not entrypoint_error,
        "enabled": enabled,
        "enabled_by_default": enabled,
        "deferred": deferred,
        "status": status,
        "provides": [],
        "requires_env": list(missing_env or []),
        "missing_env": list(missing_env or []),
        "registered": {
            "tools": 0,
            "skills": 0,
            "workflows": 0,
            "platforms": 0,
            "mcp_servers": 0,
            "hooks": 0,
            "commands": 0,
            "middleware": 0,
        },
        "registered_items": {
            "tools": [],
            "skills": [],
            "workflows": [],
            "platforms": [],
            "mcp_servers": [],
            "hooks": [],
            "commands": [],
            "middleware": [],
        },
        "entrypoint_error": entrypoint_error,
        "error": error,
        "error_traceback": "",
        "path": "",
    }
