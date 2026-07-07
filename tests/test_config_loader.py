"""Registry-driven config loader."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_config_loader_uses_defaults(tmp_path):
    from personal_agent.config_loader import ConfigLoader

    snapshot = ConfigLoader(base_dir=tmp_path).load()

    assert snapshot.attr_values["llm_provider"] == "deepseek"
    assert snapshot.attr_values["agent_data_dir"] == Path("./data")
    assert snapshot.attr_values["multimodal_text_extract_max_chars"] == 12000
    assert snapshot.attr_values["multimodal_text_extract_pdf_max_pages"] == 20
    assert snapshot.attr_values["multimodal_image_text_mode"] == "auto"
    assert snapshot.attr_values["multimodal_image_text_cache"] is True
    assert snapshot.attr_values["multimodal_image_text_max_chars"] == 6000
    assert snapshot.attr_values["multimodal_image_text_provider"] == ""
    assert snapshot.attr_values["multimodal_image_text_api_mode"] == "auto"
    assert snapshot.attr_values["multimodal_image_text_api_key"] == ""
    assert snapshot.attr_values["multimodal_ocr_endpoint"] == ""
    assert snapshot.attr_values["multimodal_ocr_timeout_seconds"] == 20
    assert snapshot.attr_values["multimodal_ocr_language"] == "auto"
    assert snapshot.sources["LLM_PROVIDER"] == "default"
    assert snapshot.source_counts["default"] == snapshot.field_count


def test_config_loader_resolves_env_yaml_and_overrides(tmp_path):
    from personal_agent.config_loader import ConfigLoader

    (tmp_path / ".env").write_text(
        "LLM_PROVIDER=openai\nLLM_MAX_TOKENS=2048\nIMAGE_TEXT_API_KEY=vision-key\nIMAGE_TEXT_API_MODE=codex_responses\n",
        encoding="utf-8",
    )
    (tmp_path / "config.yaml").write_text(
        """
storage:
  data_dir: ./runtime-data
gateway:
  platform_send_max_retries: 5
attachments:
  resolve_inbound: false
  download_platform_files: false
multimodal:
  text_extract_max_chars: 4096
  text_extract_pdf_max_pages: 3
  image_text_mode: "off"
  image_text_cache: false
  image_text_max_chars: 2048
  image_text_provider: openai
  image_text_api_mode: chat_completions
  image_text_model: gpt-4o-mini
  ocr_endpoint: http://127.0.0.1:7788
  ocr_timeout_seconds: 5
  ocr_language: zh
sandbox:
  roots: ./data,./workspace
  bash_allow_network: yes
plugins:
  dirs: ./plugins,./more-plugins
""".strip(),
        encoding="utf-8",
    )

    snapshot = ConfigLoader(base_dir=tmp_path).load(
        overrides={"platform_send_max_retries": 7}
    )

    assert snapshot.attr_values["llm_provider"] == "openai"
    assert snapshot.attr_values["llm_max_tokens"] == 2048
    assert snapshot.attr_values["agent_data_dir"] == Path("./runtime-data")
    assert snapshot.attr_values["platform_send_max_retries"] == 7
    assert snapshot.attr_values["attachments_resolve_inbound"] is False
    assert snapshot.attr_values["attachments_download_platform_files"] is False
    assert snapshot.attr_values["multimodal_text_extract_max_chars"] == 4096
    assert snapshot.attr_values["multimodal_text_extract_pdf_max_pages"] == 3
    assert snapshot.attr_values["multimodal_image_text_mode"] == "off"
    assert snapshot.attr_values["multimodal_image_text_cache"] is False
    assert snapshot.attr_values["multimodal_image_text_max_chars"] == 2048
    assert snapshot.attr_values["multimodal_image_text_provider"] == "openai"
    assert snapshot.attr_values["multimodal_image_text_api_mode"] == "codex_responses"
    assert snapshot.attr_values["multimodal_image_text_model"] == "gpt-4o-mini"
    assert snapshot.attr_values["multimodal_image_text_api_key"] == "vision-key"
    assert snapshot.attr_values["multimodal_ocr_endpoint"] == "http://127.0.0.1:7788"
    assert snapshot.attr_values["multimodal_ocr_timeout_seconds"] == 5
    assert snapshot.attr_values["multimodal_ocr_language"] == "zh"
    assert snapshot.attr_values["sandbox_roots"] == [Path("./data"), Path("./workspace")]
    assert snapshot.attr_values["bash_allow_network"] is True
    assert snapshot.attr_values["plugins_dirs"] == [Path("./plugins"), Path("./more-plugins")]
    assert snapshot.sources["LLM_PROVIDER"] == ".env"
    assert snapshot.sources["storage.data_dir"] == "config.yaml"
    assert snapshot.sources["gateway.platform_send_max_retries"] == "override"


def test_config_loader_merges_profiles_env_over_yaml(tmp_path):
    from personal_agent.config_loader import ConfigLoader

    (tmp_path / ".env").write_text(
        'PROFILES={"wechat:1":"env-profile","telegram:2":"friend"}\n',
        encoding="utf-8",
    )
    (tmp_path / "config.yaml").write_text(
        """
profiles:
  "wechat:1": yaml-profile
  local: default-profile
""".strip(),
        encoding="utf-8",
    )

    snapshot = ConfigLoader(base_dir=tmp_path).load()

    assert snapshot.attr_values["profile_map"] == {
        "wechat:1": "env-profile",
        "telegram:2": "friend",
        "local": "default-profile",
    }
    assert snapshot.sources["profiles"] == ".env"


def test_config_loader_masks_sensitive_values(tmp_path):
    from personal_agent.config_loader import ConfigLoader

    (tmp_path / ".env").write_text("LLM_API_KEY=secret\n", encoding="utf-8")

    snapshot = ConfigLoader(base_dir=tmp_path).load()
    fields = {item["path"]: item for item in snapshot.as_dict()["fields"]}

    assert snapshot.attr_values["llm_api_key"] == "secret"
    assert snapshot.as_dict()["values"]["LLM_API_KEY"] == "<set>"
    assert snapshot.as_dict()["attr_values"]["llm_api_key"] == "<set>"
    assert fields["LLM_API_KEY"]["value"] == "<set>"
    assert snapshot.as_dict()["raw_env"]["LLM_API_KEY"] == "<set>"
    assert "'LLM_API_KEY': 'secret'" not in str(snapshot.as_dict())


def test_config_loader_collects_errors_and_strict_raises(tmp_path):
    from personal_agent.config_loader import ConfigLoader, ConfigLoaderError

    (tmp_path / ".env").write_text("LLM_MAX_TOKENS=nope\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        """
gateway:
  platform_reconnect_delays: 1,no
sandbox:
  bash_allow_network: maybe
""".strip(),
        encoding="utf-8",
    )

    snapshot = ConfigLoader(base_dir=tmp_path).load(strict=False)

    assert any("LLM_MAX_TOKENS 必须是整数" in error for error in snapshot.errors)
    assert any("gateway.platform_reconnect_delays 必须是整数" in error for error in snapshot.errors)
    assert any("sandbox.bash_allow_network 必须是 true/false" in error for error in snapshot.errors)
    assert snapshot.sources["LLM_MAX_TOKENS"] == "invalid"
    with pytest.raises(ConfigLoaderError):
        ConfigLoader(base_dir=tmp_path).load(strict=True)


def test_settings_uses_config_loader(tmp_path, monkeypatch):
    from personal_agent.config import Settings

    (tmp_path / ".env").write_text("LLM_PROVIDER=anthropic\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        """
storage:
  data_dir: ./loaded-data
sandbox:
  roots: [./loaded-data]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = Settings(agent_data_dir="./override-data")

    assert settings.llm_provider == "anthropic"
    assert settings.agent_data_dir == Path("./override-data")
    assert settings.sandbox_roots == [Path("./loaded-data")]
    assert settings.config_snapshot.sources["storage.data_dir"] == "override"
