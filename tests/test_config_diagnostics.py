"""Configuration diagnostics."""

from __future__ import annotations

from pathlib import Path

from personal_agent.config_diagnostics import build_config_report, ensure_config_dirs


def test_config_report_detects_missing_env_dirs_and_unknown_keys(tmp_path):
    (tmp_path / "config.yaml").write_text(
        """
unknown_section: true
storage:
  data_dir: ./data
plugins:
  dirs:
    - ./plugins
sandbox:
  roots:
    - ./data
  bash_work_dir: ./data
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("LLM_PROVIDER=deepseek\n", encoding="utf-8")

    report = build_config_report(tmp_path)

    assert report["ok"] is False
    assert report["unknown_keys"] == ["unknown_section"]
    assert report["env"]["missing_llm_env"] == ["LLM_API_KEY"]
    assert any(item["kind"] == "data_dir" and not item["exists"] for item in report["directories"])
    assert any("未知 config 顶层配置" in warning for warning in report["warnings"])
    assert "编辑 .env，填写 LLM_API_KEY" in report["recommended_commands"]
    assert any("确认或移除未知顶层配置" in hint for hint in report["migration_hints"])


def test_config_report_recommends_copy_env_when_env_missing(tmp_path):
    (tmp_path / "config.yaml").write_text(
        """
storage:
  data_dir: ./data
sandbox:
  roots: [./data]
  bash_work_dir: ./data
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env.example").write_text("LLM_API_KEY=\n", encoding="utf-8")

    report = build_config_report(tmp_path)

    assert "personal-agent init --copy-env" in report["recommended_commands"]
    assert "cp .env.example .env" in report["recommended_commands"]


def test_config_report_validates_execution_mode(tmp_path):
    (tmp_path / "config.yaml").write_text(
        """
execution:
  mode: invalid
storage:
  data_dir: ./data
sandbox:
  roots: [./data]
  bash_work_dir: ./data
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("LLM_PROVIDER=deepseek\nLLM_API_KEY=test\n", encoding="utf-8")

    report = build_config_report(tmp_path)

    assert any("execution.mode 不支持" in error for error in report["errors"])


def test_config_report_includes_registry_field_summary(tmp_path):
    (tmp_path / "config.yaml").write_text(
        """
storage:
  data_dir: ./data
sandbox:
  roots: [./data]
  bash_work_dir: ./data
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("LLM_PROVIDER=deepseek\nLLM_API_KEY=test\n", encoding="utf-8")

    report = build_config_report(tmp_path)

    registry = report["registry_fields"]
    assert registry["field_count"] > 0
    assert registry["config_yaml_field_count"] > 0
    assert registry["env_field_count"] > 0
    assert report["registry_schema"]["version"] == 1
    assert report["registry_schema"]["field_count"] == registry["field_count"]
    assert report["registry_snapshot"]["field_count"] == registry["field_count"]
    assert report["registry_source_counts"]
    assert report["registry_coverage"]["config_yaml_field_count"] > 0
    assert "storage" in report["registry_coverage"]["present_config_sections"]
    assert "gateway" in registry["sections"]
    assert any(
        item["path"] == "gateway.platform_send_max_retries"
        for item in registry["sections"]["gateway"]
    )


def test_config_report_includes_registry_validation_errors(tmp_path):
    (tmp_path / "config.yaml").write_text(
        """
gateway:
  platform_send_max_retries: -1
sandbox:
  bash_allow_network: "yes"
  roots: [./data]
  bash_work_dir: ./data
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("LLM_PROVIDER=deepseek\nLLM_API_KEY=test\n", encoding="utf-8")

    report = build_config_report(tmp_path)

    assert any(
        "gateway.platform_send_max_retries 必须大于等于 0" in error
        for error in report["registry_validation_errors"]
    )
    assert any(
        "sandbox.bash_allow_network 必须是 true/false" in error
        for error in report["registry_validation_errors"]
    )
    assert set(report["registry_validation_errors"]).issubset(set(report["errors"]))


def test_config_report_accepts_execution_policy_overrides(tmp_path):
    (tmp_path / "config.yaml").write_text(
        """
execution:
  mode: standard
  policy:
    background: allow
    tool_permissions:
      bash: ask
      network: deny
storage:
  data_dir: ./data
sandbox:
  roots: [./data]
  bash_work_dir: ./data
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("LLM_PROVIDER=deepseek\nLLM_API_KEY=test\n", encoding="utf-8")

    report = build_config_report(tmp_path)

    assert not any("execution.policy" in error for error in report["errors"])


def test_config_report_rejects_invalid_execution_policy_overrides(tmp_path):
    (tmp_path / "config.yaml").write_text(
        """
execution:
  mode: standard
  policy:
    background: maybe
    sandbox:
      path_roots_enforced: false
    tool_permissions:
      unknown: allow
      bash: always
storage:
  data_dir: ./data
sandbox:
  roots: [./data]
  bash_work_dir: ./data
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("LLM_PROVIDER=deepseek\nLLM_API_KEY=test\n", encoding="utf-8")

    report = build_config_report(tmp_path)

    assert any("execution.policy.background 必须是 allow/ask/deny" in error for error in report["errors"])
    assert any("execution.policy.sandbox 暂不支持" in error for error in report["errors"])
    assert any("execution.policy.tool_permissions.unknown 不支持" in error for error in report["errors"])
    assert any("execution.policy.tool_permissions.bash 必须是 allow/ask/deny" in error for error in report["errors"])


def test_config_report_reports_deprecated_keys_and_platform_env(tmp_path):
    (tmp_path / "config.yaml").write_text(
        """
llm:
  provider: old
platforms:
  telegram: {}
plugins:
  enabled:
    - platforms/telegram
storage:
  data_dir: ./data
sandbox:
  roots: [./data]
  bash_work_dir: ./data
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "LLM_PROVIDER=deepseek\nLLM_API_KEY=test\nLLM_BASE_URL=https://api.deepseek.com\nLLM_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )

    report = build_config_report(tmp_path)

    assert [item["key"] for item in report["deprecated_keys"]] == ["platforms"]
    assert "llm.provider" in report["unknown_nested_keys"]
    assert any("LLM_PROVIDER" in hint for hint in report["migration_hints"])
    assert any("platforms/telegram" in hint or "平台 telegram" in hint for hint in report["migration_hints"])
    assert any("平台 telegram 缺少环境变量" in warning for warning in report["warnings"])


def test_config_report_reports_qq_platform_env(tmp_path):
    (tmp_path / "config.yaml").write_text(
        """
plugins:
  enabled:
    - platforms/qq
storage:
  data_dir: ./data
sandbox:
  roots: [./data]
  bash_work_dir: ./data
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "LLM_PROVIDER=deepseek\nLLM_API_KEY=test\n",
        encoding="utf-8",
    )

    report = build_config_report(tmp_path)

    qq = next(item for item in report["env"]["platforms"] if item["name"] == "qq")
    assert qq["enabled"] is True
    assert qq["key"] == "platforms/qq"
    assert qq["required_env"] == ["QQ_BOT_BASE_URL"]
    assert qq["configured"] is False
    assert qq["status"] == "incomplete"
    assert qq["missing_env"] == ["QQ_BOT_BASE_URL"]
    assert "QQ_BOT_BASE_URL" in qq["hint"]
    assert any("平台 qq 缺少环境变量" in warning for warning in report["warnings"])


def test_config_report_validates_nested_keys_ranges_and_env(tmp_path):
    (tmp_path / "config.yaml").write_text(
        """
agent:
  max_iterations: 0
  max_tool_calls_per_turn: many
  surprise: true
agents:
  max_concurrent_runs: -1
compression:
  engine: magic
  threshold_ratio: 1.5
memory:
  external_provider: vector
plugins:
  enabled: builtin/tools
sandbox:
  bash_restrict_paths: "yes"
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        (
            "LLM_PROVIDER=unknown\n"
            "LLM_API_KEY=test\n"
            "LLM_API_MODE=bad\n"
            "LLM_MAX_TOKENS=nope\n"
            "LLM_CONTEXT_WINDOW=-1\n"
        ),
        encoding="utf-8",
    )

    report = build_config_report(tmp_path)

    assert report["ok"] is False
    assert "agent.surprise" in report["unknown_nested_keys"]
    assert any("LLM_PROVIDER 不支持" in error for error in report["errors"])
    assert any("LLM_API_MODE 不支持" in error for error in report["errors"])
    assert any("LLM_MAX_TOKENS 必须是正整数" in error for error in report["errors"])
    assert any("LLM_CONTEXT_WINDOW 必须大于等于 0" in error for error in report["errors"])
    assert any("agent.max_iterations 必须大于 0" in error for error in report["errors"])
    assert any("agent.max_tool_calls_per_turn 必须是正整数" in error for error in report["errors"])
    assert any("compression.engine 不支持" in error for error in report["errors"])
    assert any("memory.external_provider 不支持" in error for error in report["errors"])
    assert any("plugins.enabled 必须是字符串列表" in error for error in report["errors"])


def test_config_report_accepts_gateway_and_embedding_settings(tmp_path):
    (tmp_path / "data" / "system").mkdir(parents=True)
    (tmp_path / ".env").write_text(
        "LLM_PROVIDER=deepseek\nLLM_API_KEY=test\nLLM_BASE_URL=https://api.deepseek.com\nLLM_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )
    (tmp_path / "config.yaml").write_text(
        """
storage:
  data_dir: ./data
gateway:
  platform_reconnect_delays: [2, 4, 8]
  platform_pending_warning_threshold: 12
  platform_chat_locks_maxsize: 32
  platform_message_dedupe_max_size: 2048
  platform_send_max_retries: 0
memory:
  provider: file
  external_provider: embedding
  review_interval: 10
  embedding:
    model: demo-model
    relevance_threshold: 0.25
    max_prefetch: 5
    chunk_size: 512
sandbox:
  roots: [./data]
  bash_work_dir: ./data
""".strip(),
        encoding="utf-8",
    )

    report = build_config_report(tmp_path)

    assert report["ok"] is True
    assert "gateway.platform_reconnect_delays" not in report["unknown_nested_keys"]
    assert "memory.embedding.model" not in report["unknown_nested_keys"]
    assert report["errors"] == []
    assert report["warnings"] == []


def test_config_report_accepts_codex_responses_api_mode(tmp_path):
    (tmp_path / "data" / "system").mkdir(parents=True)
    (tmp_path / ".env").write_text(
        "\n".join([
            "LLM_PROVIDER=openai",
            "LLM_API_KEY=test",
            "LLM_BASE_URL=https://api.ahooqq.cn",
            "LLM_MODEL=gpt-5.5",
            "LLM_API_MODE=codex_responses",
            "LLM_CONTEXT_WINDOW=1000000",
            "LLM_REASONING_EFFORT=high",
        ]),
        encoding="utf-8",
    )
    (tmp_path / "config.yaml").write_text(
        "storage:\n  data_dir: ./data\n",
        encoding="utf-8",
    )

    report = build_config_report(tmp_path)

    assert report["ok"] is True
    assert report["env"]["llm_context_window"] == "1000000"
    assert report["env"]["llm_reasoning_effort"] == "high"
    assert not any("LLM_API_MODE 不支持" in error for error in report["errors"])
    assert report["errors"] == []
    assert report["warnings"] == []


def test_config_report_warns_about_windows_paths_and_does_not_create_them(tmp_path):
    (tmp_path / "config.yaml").write_text(
        r"""
storage:
  data_dir: 'C:\Users\agent\data'
plugins:
  dirs:
    - 'plugins\user'
sandbox:
  roots:
    - 'D:\agent\data'
  bash_work_dir: '\\server\share\agent'
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "LLM_PROVIDER=deepseek\nLLM_API_KEY=test\nLLM_BASE_URL=https://api.deepseek.com\nLLM_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )

    report = build_config_report(tmp_path)

    assert any("Windows 盘符路径" in warning for warning in report["path_warnings"])
    assert any("UNC 路径" in warning for warning in report["path_warnings"])
    assert any("反斜杠路径" in warning for warning in report["path_warnings"])
    assert any(not item["portable"] for item in report["directories"])
    assert ensure_config_dirs(tmp_path) == []
    assert not (tmp_path / r"C:\Users\agent\data").exists()


def test_config_report_diagnoses_mcp_servers(tmp_path):
    (tmp_path / "config.yaml").write_text(
        """
storage:
  data_dir: ./data
sandbox:
  roots: [./data]
  bash_work_dir: ./data
mcp:
  enabled: true
  servers:
    - name: missing-command
      enabled: true
    - name: missing-binary
      command: definitely_missing_personal_agent_mcp
      extra: ignored
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "LLM_PROVIDER=deepseek\nLLM_API_KEY=test\nLLM_BASE_URL=https://api.deepseek.com\nLLM_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )

    report = build_config_report(tmp_path)

    assert any("MCP 服务器 missing-command 缺少 command" in error for error in report["errors"])
    assert any("MCP 服务器 missing-binary 的命令不可用" in warning for warning in report["warnings"])
    assert report["mcp_servers"][0]["missing_command"] is True
    assert report["mcp_servers"][1]["unknown_keys"] == ["extra"]


def test_ensure_config_dirs_creates_expected_directories(tmp_path):
    (tmp_path / "config.yaml").write_text(
        """
storage:
  data_dir: ./data
plugins:
  dirs: [./plugins, ./data/plugins]
sandbox:
  roots: [./data]
  bash_work_dir: ./data
""".strip(),
        encoding="utf-8",
    )

    created = ensure_config_dirs(tmp_path)

    assert tmp_path / "data" in [Path(item) for item in created]
    assert (tmp_path / "data" / "system").exists()
    assert (tmp_path / "plugins").exists()
    assert (tmp_path / "data" / "plugins").exists()
    assert ensure_config_dirs(tmp_path) == []
