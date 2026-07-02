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
