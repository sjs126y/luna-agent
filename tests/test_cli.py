"""CLI diagnostics and token commands."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from personal_agent.cli import app, build_doctor_report, format_plugin_report


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
    assert "错误: boom" in text
    assert "Traceback demo" in text
