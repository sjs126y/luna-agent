"""CLI diagnostics and token commands."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from personal_agent.agents.runtime import AgentRun
from personal_agent.cli import (
    app,
    build_doctor_report,
    format_config_report,
    format_doctor_report,
    format_memory_doctor,
    format_memory_entries,
    format_plugin_list,
    format_plugin_report,
    format_plugin_validation_report,
)


runner = CliRunner()


def test_tokens_estimate_command_outputs_number():
    result = runner.invoke(app, ["tokens", "estimate", "hello"])

    assert result.exit_code == 0
    assert result.output.strip().isdigit()


def test_protocol_schema_command_outputs_frontend_contract():
    result = runner.invoke(app, ["protocol", "schema", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["protocol_version"] == 1
    assert "assistant_delta" in data["delta_event_types"]
    assert data["events"]["tool_decision"]["type"] == "tool_decision"
    fields = {
        field["name"]: field
        for field in data["events"]["tool_decision"]["fields"]
    }
    assert fields["tool_name"]["required"] is True
    assert fields["display_name"]["type"] == "string"
    assert fields["available_actions"]["type"] == "list[string]"


def test_doctor_report_includes_execution_policy():
    from personal_agent.config import Settings

    settings = Settings(
        execution_mode="sovereign",
        execution_policy_overrides={"background": "ask"},
        llm_api_key="test",
        llm_base_url="https://example.test",
    )
    report = build_doctor_report(settings)
    summary = format_doctor_report(report)
    text = format_doctor_report(report, verbose=True)

    assert report["execution"]["mode"] == "sovereign"
    assert report["execution"]["isolation"] == "tool-enforced"
    assert report["execution"]["profile"]["name"] == "sovereign"
    assert report["execution"]["profile"]["sandbox"]["hard_prechecks_enforced"] is True
    assert report["execution"]["permissions"]["background"] == "ask"
    assert report["execution"]["overrides"]["tool_permissions"]["background"] == "ask"
    assert report["effective_config"]["field_count"] > 0
    effective_fields = {item["path"]: item for item in report["effective_config"]["fields"]}
    assert effective_fields["execution.mode"]["value"] == "sovereign"
    assert effective_fields["LLM_API_KEY"]["value"] == "<set>"
    assert report["tools"]["total"] >= 0
    assert "by_permission" in report["tools"]
    assert "Lumora doctor" in summary
    assert "工具:" in summary
    assert "doctor --verbose" in summary
    assert "Effective Config:" not in summary
    assert "Execution:" in text
    assert "Tools:" in text
    assert "by risk:" in text
    assert "mode: sovereign" in text
    assert "profile: Sovereign" in text
    assert "Effective Config:" in text
    assert "execution.mode: sovereign" in text
    assert "LLM_API_KEY: <set>" in text
    assert "effective permissions:" in text
    assert "overrides: background=ask" in text
    assert "hard prechecks: enforced" in text
    assert "grants: turn scoped /allow" in text
    assert "warning:" in text


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


def test_chat_without_message_runs_inline_tui(monkeypatch):
    calls = []

    def fake_inline(*, session_name="default"):
        calls.append(session_name)

    monkeypatch.setattr("personal_agent.tui.app.run_inline_tui_sync", fake_inline)
    result = runner.invoke(app, ["chat", "--session", "work"])

    assert result.exit_code == 0
    assert calls == ["work"]


def test_chat_ui_classic_is_removed():
    result = runner.invoke(app, ["chat", "--ui", "classic"])

    assert result.exit_code == 1
    assert "classic UI 已移除" in result.output


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
            "schema_version": 3,
            "run_id": "abc123",
            "status": "completed",
            "role": "reviewer",
            "task": "inspect",
        }],
    )
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.list_active_agent_runs",
        lambda: [{
            "run_id": "active1",
            "status": "running",
            "role": "researcher",
            "task": "active",
            "active": True,
        }],
    )
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.get_agent_run",
        lambda run_id: run if run_id == "abc123" else None,
    )

    listed = runner.invoke(app, ["agents", "list", "--json"])
    shown = runner.invoke(app, ["agents", "show", "abc123", "--json"])

    assert listed.exit_code == 0
    listed_data = json.loads(listed.output)
    assert listed_data["runs"][0]["run_id"] == "abc123"
    assert listed_data["active_runs"][0]["run_id"] == "active1"
    assert shown.exit_code == 0
    data = json.loads(shown.output)
    assert data["schema_version"] == 3
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
    assert data["schema_version"] == 3


def test_agents_show_missing_outputs_chinese_error(monkeypatch):
    monkeypatch.setattr("personal_agent.cli._load_agent_run_store", lambda: None)
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.get_agent_run",
        lambda run_id: None,
    )

    result = runner.invoke(app, ["agents", "show", "missing"])

    assert result.exit_code == 1
    assert "错误: 未找到子 agent 运行记录: missing" in result.stderr


def test_memory_cli_commands(monkeypatch):
    async def report():
        return {
            "builtin_available": True,
            "builtin_provider": "internal_markdown",
            "external_available": False,
            "external_provider": "",
            "provider": "file",
            "external_provider_config": "none",
            "review_service": "MemoryReviewService",
            "review_enabled": True,
            "providers": {
                "builtin": {"provider": "internal_markdown", "available": True, "entries": 1},
                "external": {"provider": "", "available": False, "entries": 0},
            },
            "review": {"enabled": True, "active": False, "spawn_count": 1, "saved_count": 0},
            "last_errors": {},
        }

    async def entries(target="all"):
        return [{"id": "memory:1", "index": 1, "provider": "builtin", "target": "memory", "text": "hello"}]

    async def search(query, target="all"):
        return [{"id": "memory:1", "index": 1, "provider": "builtin", "target": "memory", "text": query}]

    async def entry(identifier, target="all"):
        return {"id": identifier, "provider": "builtin", "target": "memory", "text": "hello"}

    async def delete(identifier, target="all"):
        return identifier == "memory:1"

    monkeypatch.setattr("personal_agent.cli._memory_report", report)
    monkeypatch.setattr("personal_agent.cli._memory_entries", entries)
    monkeypatch.setattr("personal_agent.cli._memory_search_entries", search)
    monkeypatch.setattr("personal_agent.cli._memory_entry", entry)
    monkeypatch.setattr("personal_agent.cli._memory_delete", delete)

    assert runner.invoke(app, ["memory", "doctor"]).exit_code == 0
    listed = runner.invoke(app, ["memory", "list", "--json"])
    searched = runner.invoke(app, ["memory", "search", "needle"])
    shown = runner.invoke(app, ["memory", "show", "memory:1"])
    deleted = runner.invoke(app, ["memory", "delete", "memory:1", "--yes"])

    assert json.loads(listed.output)[0]["id"] == "memory:1"
    assert "needle" in searched.output
    assert "记忆: memory:1" in shown.output
    assert "已删除记忆: memory:1" in deleted.output


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


def test_plugins_validate_command_loads_local_plugin(tmp_path):
    plugin_dir = tmp_path / "plugins" / "clihello"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/clihello
name: CLI Hello
version: 1.0.0
entrypoint: clihello:register
enabled_by_default: false
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        """
from personal_agent.plugins.models import CommandEntry

def hello(args="", **kwargs):
    return "hello"

def register(ctx):
    ctx.register_command(CommandEntry(
        name="clihello",
        description="CLI validation command",
        handler=hello,
        scope="both",
    ))
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["plugins", "validate", str(plugin_dir)])

    assert result.exit_code == 0
    assert "插件校验" in result.output
    assert "校验结果: 通过" in result.output
    assert "commands: clihello" in result.output


def test_plugins_validate_json_reports_bad_manifest(tmp_path):
    plugin_dir = tmp_path / "plugins" / "bad"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: User/Bad
name: Bad
version: 1.0.0
entrypoint: bad:register
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["plugins", "validate", str(plugin_dir), "--json"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["key"] == "invalid/bad"
    assert data["validation_ok"] is False
    assert data["manifest_valid"] is False
    assert "field 'key'" in data["manifest_error"]


def test_global_doctor_report_contains_tokenizer_and_plugins():
    report = build_doctor_report()

    assert "runtime" in report
    assert "memory" in report
    assert "mcp_runtime" in report
    assert "initialized" in report["runtime"]
    assert "builtin_available" in report["memory"]
    assert "tokenizer" in report
    assert "tiktoken_available" in report["tokenizer"]
    assert any(plugin["key"] == "builtin/tools" for plugin in report["plugins"])


def test_global_doctor_json_command_contains_runtime_and_memory():
    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "runtime" in data
    assert "memory" in data
    assert "gateway" in data
    assert "mcp_runtime" in data
    assert "config" in data
    assert "agents" in data
    assert "plugins" in data


def test_doctor_report_includes_runtime_failure(monkeypatch):
    def failed_runtime(settings):
        return {
            "runtime": {
                "initialized": False,
                "error": "RuntimeError: broken",
                "db_open": False,
                "mcp_running": False,
                "gateway_created": False,
                "gateway_running": False,
                "cached_agents": 0,
            },
            "memory": {
                "builtin_available": False,
                "builtin_provider": "",
                "external_provider": "",
                "review_service": "",
                "review_enabled": False,
            },
            "_plugins": [],
        }

    monkeypatch.setattr("personal_agent.cli._runtime_health_report", failed_runtime)

    report = build_doctor_report()
    text = format_doctor_report(report)
    verbose_text = format_doctor_report(report, verbose=True)

    assert report["runtime"]["initialized"] is False
    assert "Runtime 初始化失败: RuntimeError: broken" in text
    assert "内置 memory provider 不可用" in verbose_text
    assert "Lumora doctor" in text
    assert "状态: 不可用，需要处理" in text


def test_init_command_generates_and_skips_existing_files(tmp_path):
    result = runner.invoke(app, ["init", "--dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "初始化 Personal Agent 配置" in result.output
    assert "已生成" in result.output
    assert (tmp_path / "config.yaml").exists()
    assert (tmp_path / ".env.example").exists()
    assert "agents:" in (tmp_path / "config.yaml").read_text(encoding="utf-8")

    original = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    second = runner.invoke(app, ["init", "--dir", str(tmp_path)])

    assert second.exit_code == 0
    assert "已跳过" in second.output
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == original


def test_init_profiles_generate_distinct_templates(tmp_path):
    local_dir = tmp_path / "local"
    bot_dir = tmp_path / "bot"
    telegram_dir = tmp_path / "telegram"

    local = runner.invoke(app, ["init", "--dir", str(local_dir), "--profile", "local"])
    bot = runner.invoke(app, ["init", "--dir", str(bot_dir), "--profile", "bot"])
    telegram = runner.invoke(app, ["init", "--dir", str(telegram_dir), "--profile", "telegram"])

    assert local.exit_code == 0
    assert bot.exit_code == 0
    assert telegram.exit_code == 0
    assert "enabled: false" in (local_dir / "config.yaml").read_text(encoding="utf-8")
    bot_config = (bot_dir / "config.yaml").read_text(encoding="utf-8")
    assert "# Personal Agent bot configuration" in bot_config
    assert "enabled: true" in bot_config
    assert "# Telegram" in (bot_dir / ".env.example").read_text(encoding="utf-8")
    telegram_config = (telegram_dir / "config.yaml").read_text(encoding="utf-8")
    telegram_env = (telegram_dir / ".env.example").read_text(encoding="utf-8")
    assert "# Personal Agent telegram bot configuration" in telegram_config
    assert "platforms/telegram" in telegram_config
    assert "TELEGRAM_BOT_TOKEN" in telegram_env
    assert "FEISHU_APP_ID" not in telegram_env


def test_init_platform_profiles_generate_platform_specific_env(tmp_path):
    cases = {
        "feishu": ("platforms/feishu", "FEISHU_APP_ID", "TELEGRAM_BOT_TOKEN"),
        "wechat": ("platforms/wechat", "WEIXIN_ACCOUNT_ID", "FEISHU_APP_ID"),
        "qq": ("platforms/qq", "QQ_BOT_BASE_URL", "FEISHU_APP_ID"),
    }
    for profile, (plugin_key, expected_env, absent_env) in cases.items():
        target = tmp_path / profile
        result = runner.invoke(app, ["init", "--dir", str(target), "--profile", profile])

        assert result.exit_code == 0
        assert plugin_key in (target / "config.yaml").read_text(encoding="utf-8")
        env_example = (target / ".env.example").read_text(encoding="utf-8")
        assert expected_env in env_example
        assert absent_env not in env_example


def test_init_copy_env_creates_and_respects_existing_env(tmp_path):
    result = runner.invoke(app, ["init", "--dir", str(tmp_path), "--profile", "telegram", "--copy-env"])

    assert result.exit_code == 0
    env_path = tmp_path / ".env"
    assert env_path.exists()
    assert "TELEGRAM_BOT_TOKEN" in env_path.read_text(encoding="utf-8")

    env_path.write_text("CUSTOM=1\n", encoding="utf-8")
    second = runner.invoke(app, ["init", "--dir", str(tmp_path), "--profile", "telegram", "--copy-env"])

    assert second.exit_code == 0
    assert "已跳过" in second.output
    assert env_path.read_text(encoding="utf-8") == "CUSTOM=1\n"

    forced = runner.invoke(app, ["init", "--dir", str(tmp_path), "--profile", "telegram", "--copy-env", "--force"])

    assert forced.exit_code == 0
    assert "已覆盖" in forced.output
    assert "TELEGRAM_BOT_TOKEN" in env_path.read_text(encoding="utf-8")


def test_init_check_reports_missing_config_without_writing(tmp_path):
    result = runner.invoke(app, ["init", "--check", "--dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "配置检查" in result.output
    assert "缺少 config.yaml" in result.output
    assert not (tmp_path / "config.yaml").exists()


def test_init_fix_dirs_creates_base_directories(tmp_path):
    result = runner.invoke(app, ["init", "--dir", str(tmp_path), "--fix-dirs"])

    assert result.exit_code == 0
    assert (tmp_path / "data").exists()
    assert (tmp_path / "data" / "system").exists()
    assert (tmp_path / "plugins").exists()
    assert (tmp_path / "data" / "plugins").exists()


def test_init_command_force_overwrites_existing_files(tmp_path):
    config = tmp_path / "config.yaml"
    env = tmp_path / ".env.example"
    config.write_text("old", encoding="utf-8")
    env.write_text("old", encoding="utf-8")

    result = runner.invoke(app, ["init", "--dir", str(tmp_path), "--force"])

    assert result.exit_code == 0
    assert "已覆盖" in result.output
    assert "storage:" in config.read_text(encoding="utf-8")
    assert "LLM_PROVIDER" in env.read_text(encoding="utf-8")


def test_format_config_report_shows_next_steps():
    report = {
        "ok": False,
        "base_dir": "demo",
        "files": {
            "config": {"exists": False, "path": "demo/config.yaml"},
            "env": {"exists": False, "path": "demo/.env"},
            "env_example": {"exists": True, "path": "demo/.env.example"},
        },
        "env": {
            "llm_provider": "deepseek",
            "llm_api_key_set": False,
            "llm_base_url_set": False,
            "llm_model_set": False,
            "missing_llm_env": ["LLM_API_KEY"],
            "platforms": [{
                "key": "platforms/qq",
                "name": "qq",
                "enabled": True,
                "configured": False,
                "status": "incomplete",
                "required_env": ["QQ_BOT_BASE_URL"],
                "missing_env": ["QQ_BOT_BASE_URL"],
                "hint": "填写 QQ_BOT_BASE_URL。",
            }],
        },
        "directories": [{"kind": "data_dir", "path": "demo/data", "exists": False, "required": True}],
        "registry_schema": {"version": 1, "field_count": 3},
        "registry_source_counts": {"default": 2, ".env": 1},
        "unknown_keys": ["old"],
        "deprecated_keys": [],
        "migration_hints": ["确认或移除未知顶层配置: old。"],
        "recommended_commands": ["personal-agent init"],
        "warnings": ["缺少 config.yaml。"],
        "next_steps": ["运行 personal-agent init 生成 config.yaml。"],
    }

    text = format_config_report(report)

    assert "配置检查" in text
    assert "配置字段:" in text
    assert "schema version: 1" in text
    assert "schema fields: 3" in text
    assert "source counts: .env=1, default=2" in text
    assert "known fields:" in text
    assert "config.yaml fields:" in text
    assert "未知配置: old" in text
    assert "平台:" in text
    assert "platforms/qq" in text
    assert "缺失=QQ_BOT_BASE_URL" in text
    assert "迁移建议" in text
    assert "推荐命令" in text
    assert "运行 personal-agent init" in text


def test_format_doctor_config_section_includes_grouped_effective_config():
    report = {
        "config": {
            "ok": True,
            "base_dir": "demo",
            "files": {
                "config": {"exists": True, "path": "demo/config.yaml"},
                "env": {"exists": True, "path": "demo/.env"},
                "env_example": {"exists": True, "path": "demo/.env.example"},
            },
            "env": {
                "llm_provider": "deepseek",
                "llm_api_key_set": True,
                "llm_base_url_set": True,
                "llm_model_set": True,
                "missing_llm_env": [],
                "platforms": [],
            },
            "registry_fields": {"field_count": 2, "sections": {"execution": [], "llm": []}},
            "registry_schema": {"version": 1, "field_count": 2},
            "registry_source_counts": {"default": 2},
            "registry_coverage": {
                "config_yaml_field_count": 1,
                "config_yaml_sections": ["execution"],
                "present_config_sections": ["execution"],
            },
            "directories": [],
            "warnings": [],
            "errors": [],
        },
        "effective_config": {
            "field_count": 2,
            "sections": {
                "execution": [{
                    "path": "execution.mode",
                    "value": "standard",
                    "source": "config.yaml",
                }],
                "llm": [{
                    "path": "LLM_API_KEY",
                    "value": "<set>",
                    "source": ".env",
                }],
            },
        },
    }

    text = format_doctor_report(report, section="config")

    assert "配置检查" in text
    assert "schema version: 1" in text
    assert "schema fields: 2" in text
    assert "Effective Config: 2 fields" in text
    assert "execution.mode: standard (config.yaml)" in text
    assert "LLM_API_KEY: <set> (.env)" in text


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


def test_format_plugin_report_includes_manifest_status_and_hints():
    report = _plugin_report(
        "invalid/demo",
        status="ERROR",
        enabled=False,
        manifest_valid=False,
        manifest_error="bad manifest",
        diagnostic_hints=["修复插件 manifest: bad manifest"],
    )

    text = format_plugin_report(report, include_traceback=False)

    assert "Manifest: 异常" in text
    assert "Manifest 错误: bad manifest" in text
    assert "Manifest 异常: bad manifest" in text
    assert "建议: 修复插件 manifest: bad manifest" in text


def test_format_plugin_report_includes_deferred_reason():
    report = _plugin_report(
        "platforms/demo",
        status="DEFERRED",
        kind="platform",
        deferred=True,
        deferred_reason="平台插件会在网关解析平台适配器时加载",
        diagnostic_hints=["平台插件会在网关解析平台适配器时加载"],
    )

    text = format_plugin_report(report, include_traceback=False)

    assert "延迟原因: 平台插件会在网关解析平台适配器时加载" in text
    assert "延迟加载，当前未 import" in text
    assert "建议: 平台插件会在网关解析平台适配器时加载" in text


def test_format_memory_doctor_and_entries():
    report = {
        "builtin_available": True,
        "builtin_provider": "internal_markdown",
        "external_available": False,
        "external_provider": "",
        "provider": "file",
        "external_provider_config": "none",
        "review_service": "MemoryReviewService",
        "review_enabled": True,
        "providers": {
            "builtin": {"provider": "internal_markdown", "available": True, "entries": 2, "memory_entries": 1, "user_entries": 1},
            "external": {"provider": "", "available": False, "entries": 0},
        },
        "migration": {"pending": 3},
        "index": {"pending": 1},
        "review": {"enabled": True, "active": False, "spawn_count": 2, "saved_count": 1, "last_error": ""},
        "last_errors": {},
    }
    entries = [{"id": "memory:1", "provider": "builtin", "target": "memory", "text": "hello memory"}]

    doctor_text = format_memory_doctor(report)
    list_text = format_memory_entries(entries)

    assert "Memory 诊断" in doctor_text
    assert "internal_markdown" in doctor_text
    assert "spawn count: 2" in doctor_text
    assert "migration pending: 3" in doctor_text
    assert "index pending: 1" in doctor_text
    assert "记忆列表: 1 条" in list_text
    assert "memory:1" in list_text


def test_format_plugin_validation_report_wraps_plugin_report():
    report = _plugin_report("user/demo", status="LOADED")
    report.update({
        "validation_path": "plugins/demo",
        "validation_manifest": "plugins/demo/plugin.yaml",
        "validation_load_requested": True,
        "validation_ok": True,
    })

    text = format_plugin_validation_report(report, include_traceback=False)

    assert "插件校验" in text
    assert "Manifest 文件: plugins/demo/plugin.yaml" in text
    assert "加载测试: 已执行" in text
    assert "校验结果: 通过" in text
    assert "插件: user/demo" in text


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
        "mcp_runtime": {
            "running": True,
            "configured_count": 1,
            "connected_count": 0,
            "total_tools": 0,
            "registered_tools": [],
            "servers": [{
                "name": "demo",
                "command": "missing-cmd",
                "args": [],
                "enabled": True,
                "connected": False,
                "pid": None,
                "tool_count": 0,
                "server_name": "",
                "server_version": "",
                "last_error": "command not found: missing-cmd",
                "last_call_error": "",
                "last_connected_at": "",
                "last_disconnected_at": "",
                "stderr_tail": ["startup failed"],
            }],
        },
        "runtime": {
            "initialized": True,
            "db_open": True,
            "mcp_running": True,
            "gateway_created": True,
            "gateway_running": True,
            "cached_agents": 0,
            "error": "",
            "turns": {
                "stored": 2,
                "last_status": "failed",
                "last_error": "RuntimeError: boom",
                "last_duration": 1.2345,
                "last_llm_calls": 1,
                "last_tool_calls": 3,
                "last_input_tokens": 10,
                "last_output_tokens": 5,
                "last_retries": 1,
                "persisted": {
                    "stored": 7,
                    "last_id": 42,
                    "last_turn_id": "turn-42",
                    "last_session_key": "cli:default:local",
                    "last_status": "completed",
                    "last_error": "",
                    "last_cache_hit_tokens": 4,
                    "last_cache_miss_tokens": 6,
                    "last_cache_write_tokens": 0,
                    "last_cache_read_tokens": 4,
                },
            },
            "llm_cache": {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "strategy": "prefix",
                "supports_usage": True,
                "usage_fields": {"cache_hit_tokens": "prompt_cache_hit_tokens"},
                "cacheable_blocks": ["system", "tools", "message_prefix"],
                "last_usage": {
                    "cache_hit_tokens": 4,
                    "cache_miss_tokens": 6,
                    "cache_write_tokens": 0,
                    "cache_read_tokens": 4,
                    "cache_hit_rate": 0.4,
                },
                "last_diagnostics": {
                    "system_hash": "sys",
                    "tools_hash": "tools",
                    "stable_prefix_hash": "stable",
                    "dynamic_context_hash": "dynamic",
                    "stable_block_count": 2,
                    "dynamic_block_count": 1,
                    "current_user_present": True,
                },
                "error": "",
            },
            "tool_truth": {
                "stored": 2,
                "inspected": 2,
                "turns_with_tools": 1,
                "turns_without_tools": 1,
                "claim_mismatches": 1,
                "tool_counts": {"bash": 2, "search": 1},
                "status_counts": {"success": 2, "error": 0, "denied": 1},
                "denied_tool_calls": 1,
                "failed_tool_calls": 0,
                "warning_counts": {
                    "assistant_claimed_tool_use_without_tool_call": 1,
                },
                "last_warning": "assistant_claimed_tool_use_without_tool_call",
                "last_claimed_but_no_tool_call": True,
            },
            "tool_runs": {
                "inspected": 3,
                "tool_counts": {"bash": 2, "write": 1},
                "status_counts": {"success": 2, "denied": 1},
                "category_counts": {"permission": 1},
                "denied": 1,
                "failed": 0,
                "timeouts": 0,
                "truncated": 1,
            },
            "commands": {
                "registry_version": 1,
                "core_commands": 15,
                "plugin_commands": 2,
                "argument_specs": 4,
                "dynamic_providers": ["sessions", "tools"],
                "has_tool_runs": True,
                "has_mode_arguments": True,
                "has_allow_arguments": True,
            },
            "query": {
                "conversation_query_service": True,
                "tool_runs_query": True,
            },
            "execution": {
                "mode": "standard",
                "label": "Ask First",
                "isolation": "tool-enforced",
                "network": "ask",
                "permissions": {"write": "ask", "bash": "ask"},
            },
        },
        "platforms": [{
            "key": "platforms/telegram",
            "name": "Telegram",
            "status": "DEFERRED",
            "missing_env": [],
            "enabled": True,
            "health": {
                "name": "telegram",
                "status": "reconnecting",
                "connected": False,
                "attempts": 2,
                "pending_messages": 3,
                "next_retry_at": "2026-07-02T10:00:00",
                "last_connect_error": "RuntimeError: no token",
                "last_send_error": "",
                "capabilities": {
                    "text": True,
                    "markdown": True,
                    "typing": True,
                    "max_text_length": 4096,
                },
            },
        }],
        "gateway": {
            "started": True,
            "adapter_count": 1,
            "running_agents": 0,
            "stop_requested_agents": 0,
            "longest_running_seconds": 0,
            "pending_messages": 0,
            "active_adapter_sessions": 0,
            "cron_enabled": False,
            "platforms": [{
                "name": "telegram",
                "last_connect_error": "RuntimeError: no token",
                "last_send_error": "",
            }],
        },
        "plugins": [_plugin_report("user/demo", status="ERROR", error="boom")],
        "tokenizer": {
            "tiktoken_available": True,
            "fallback_active": False,
            "default_encoding": "cl100k_base",
            "cached_encodings": {},
        },
    }

    summary_text = format_doctor_report(report)
    text = format_doctor_report(report, verbose=True)

    assert "Lumora doctor" in summary_text
    assert "状态: 可用，有提示" in summary_text
    assert "模型: deepseek / deepseek-v4-flash" in summary_text
    assert "运行时: 已就绪" in summary_text
    assert "工具:" in summary_text
    assert "需要注意:" in summary_text
    assert "doctor --verbose" in summary_text
    assert "Agents:" not in summary_text
    assert "Effective Config:" not in summary_text
    assert "总体状态: 需要注意" in text
    assert "Agents:" in text
    assert "Tools:" in text
    assert (
        "Turns: stored=2 last=failed duration=1.234s llm=1 tools=3 retries=1 "
        "persisted=7 persisted_last=completed"
    ) in text
    assert "LLM Cache: strategy=prefix usage=是 hit=4 miss=6 rate=0.40" in text
    assert "Tool Truth: inspected=2 with_tools=1 mismatches=1 denied=1" in text
    assert "Tool Runs: stored=3 denied=1 failed=0 truncated=1" in text
    assert "Commands: registry=v1 core=15 plugins=2 arguments=4 providers=sessions, tools" in text
    assert "Query: conversation=是 tool_runs=是" in text
    assert "Execution: mode=standard label=Ask First isolation=tool-enforced" in text
    assert "插件概览: 总数=1 已加载=0 延迟=0 禁用=0 错误=1" in text
    assert "需要注意:" in text
    assert "Sandbox root 不存在: /missing" in text
    assert "MCP 服务器 demo 的命令不可用: missing-cmd" in text
    assert (
        "runtime=stopped transport=stdio tools=0 reconnects=0 next_retry=- "
        "error=command not found: missing-cmd"
    ) in text
    assert "stderr: startup failed" in text
    assert "MCP 服务器 demo 连接失败: command not found: missing-cmd" in text
    assert "runtime=reconnecting connected=否 attempts=2 pending=3" in text
    assert "capabilities=text,markdown,typing,max=4096" in text
    assert "平台 telegram 连接失败: RuntimeError: no token" in text
    assert "插件 user/demo: 加载错误: boom" in text

    runtime_text = format_doctor_report(report, section="runtime")
    assert "Turns:" in runtime_text
    assert "  stored: 2" in runtime_text
    assert "  last status: failed" in runtime_text
    assert "  last tokens: in=10 out=5" in runtime_text
    assert "  last error: RuntimeError: boom" in runtime_text
    assert "  persisted stored: 7" in runtime_text
    assert "  persisted last id: 42" in runtime_text
    assert "  persisted last status: completed" in runtime_text
    assert "  persisted last session: cli:default:local" in runtime_text
    assert "  persisted last turn: turn-42" in runtime_text
    assert "  persisted last cache: hit=4 miss=6 write=0 read=4" in runtime_text
    assert "LLM Cache:" in runtime_text
    assert "  strategy: prefix" in runtime_text
    assert "  last usage: hit=4 miss=6 write=0 read=4 rate=0.40" in runtime_text
    assert "  stable prefix hash: stable" in runtime_text
    assert "  dynamic context hash: dynamic" in runtime_text
    assert "  stable blocks: 2" in runtime_text
    assert "  dynamic blocks: 1" in runtime_text
    assert "  current user present: 是" in runtime_text
    assert "Tool Truth:" in runtime_text
    assert "  inspected: 2" in runtime_text
    assert "  claim mismatches: 1" in runtime_text
    assert "  tool counts: bash=2, search=1" in runtime_text
    assert "  warnings: assistant_claimed_tool_use_without_tool_call=1" in runtime_text
    assert "Tool Runs:" in runtime_text
    assert "  stored: 3" in runtime_text
    assert "  status counts: denied=1, success=2" in runtime_text
    assert "  category counts: permission=1" in runtime_text
    assert "Commands:" in runtime_text
    assert "  /tool-runs: 是" in runtime_text
    assert "Query:" in runtime_text
    assert "  tool runs query: 是" in runtime_text
    assert "Execution:" in runtime_text
    assert "  label: Ask First" in runtime_text


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
    manifest_valid: bool = True,
    manifest_error: str = "",
    deferred_reason: str = "",
    diagnostic_hints: list[str] | None = None,
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
        "manifest_valid": manifest_valid,
        "manifest_error": manifest_error,
        "deferred_reason": deferred_reason,
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
        "diagnostic_hints": list(diagnostic_hints or []),
    }
