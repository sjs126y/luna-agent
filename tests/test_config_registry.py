"""Configuration registry behavior."""

from __future__ import annotations


def test_config_registry_paths_are_unique():
    from personal_agent.config_registry import CONFIG_FIELDS, CONFIG_REGISTRY

    paths = [field.path for field in CONFIG_FIELDS]

    assert len(paths) == len(set(paths))
    assert "execution.mode" in paths
    assert "LLM_API_KEY" in paths
    assert "LLM_REASONING_EFFORT" in paths
    assert "llm.context_window" in paths
    assert "sandbox.roots" in paths
    assert "attachments.resolve_inbound" in paths
    assert "gateway.platform_send_max_retries" in paths
    assert "profiles" in paths
    assert CONFIG_REGISTRY.get("execution.mode") is not None
    assert CONFIG_REGISTRY.get_by_attr("execution_mode") is CONFIG_REGISTRY.get("execution.mode")


def test_config_registry_keeps_field_order_and_metadata():
    from personal_agent.config_registry import CONFIG_FIELDS

    paths = [field.path for field in CONFIG_FIELDS]
    fields = {field.path: field for field in CONFIG_FIELDS}

    assert paths[:3] == ["execution.mode", "execution.policy", "LLM_PROVIDER"]
    assert paths[-1] == "profiles"
    assert fields["LLM_API_KEY"].sensitive is True
    assert fields["LLM_REASONING_EFFORT"].source == ".env"
    assert fields["llm.context_window"].env_key == "LLM_CONTEXT_WINDOW"
    assert fields["llm.context_window"].minimum == 0
    assert fields["sandbox.roots"].allow_csv is True
    assert fields["gateway.platform_send_max_retries"].minimum == 0
    assert fields["profiles"].env_key == "PROFILES"
    assert fields["profiles"].yaml_path == "profiles"


def test_config_registry_rejects_duplicate_path_and_attr():
    import pytest

    from personal_agent.config_registry import ConfigField, ConfigRegistry

    registry = ConfigRegistry((
        ConfigField("demo.enabled", "demo_enabled", "config.yaml", False, "bool", "demo", "Demo."),
    ))

    with pytest.raises(ValueError, match="Duplicate config field path"):
        registry.register(
            ConfigField("demo.enabled", "other_enabled", "config.yaml", False, "bool", "demo", "Demo.")
        )
    with pytest.raises(ValueError, match="Duplicate config field attr"):
        registry.register(
            ConfigField("demo.other", "demo_enabled", "config.yaml", False, "bool", "demo", "Demo.")
        )


def test_config_registry_supports_plugin_namespaced_fields():
    from personal_agent.config_registry import ConfigField, ConfigRegistry

    field = ConfigField(
        "plugin_config.platforms/telegram.poll_interval",
        "telegram_poll_interval",
        "config.yaml",
        2,
        "int",
        "plugin_config",
        "Telegram polling interval.",
        owner="plugin",
        namespace="plugin_config",
        plugin_key="platforms/telegram",
        minimum=1,
    )
    registry = ConfigRegistry((field,))

    schema_field = registry.schema()["fields"][0]
    assert registry.get(field.path) == field
    assert registry.yaml_known_keys_by_section()["plugin_config"] == {"platforms/telegram"}
    assert schema_field["owner"] == "plugin"
    assert schema_field["plugin_key"] == "platforms/telegram"
    assert "env_key" in schema_field
    assert "yaml_path" in schema_field
    assert "runtime_type" in schema_field


def test_config_registry_exposes_known_yaml_sections_and_keys():
    from personal_agent.config_registry import (
        CONFIG_REGISTRY,
        config_yaml_known_keys_by_section,
        config_yaml_known_sections,
    )

    sections = config_yaml_known_sections()
    keys = config_yaml_known_keys_by_section()

    assert "execution" in sections
    assert "attachments" in sections
    assert "gateway" in sections
    assert "llm" in sections
    assert "profiles" in sections
    assert "platform_send_max_retries" in keys["gateway"]
    assert "context_window" in keys["llm"]
    assert "embedding" in keys["memory"]
    assert "review" in keys["memory"]
    assert "qdrant" in keys["memory"]
    assert keys["profiles"] is None
    assert "resolve_inbound" in keys["attachments"]
    assert len(CONFIG_REGISTRY.env_fields()) > 0


def test_config_registry_validates_basic_values():
    from personal_agent.config_registry import config_field_by_path, validate_registry_value

    retries = config_field_by_path("gateway.platform_send_max_retries")
    mode = config_field_by_path("execution.mode")
    network = config_field_by_path("sandbox.bash_allow_network")
    delays = config_field_by_path("gateway.platform_reconnect_delays")

    assert retries is not None
    assert mode is not None
    assert network is not None
    assert delays is not None
    assert validate_registry_value(retries, -1)["errors"] == [
        "gateway.platform_send_max_retries 必须大于等于 0。"
    ]
    assert any("execution.mode 不支持" in error for error in validate_registry_value(mode, "bad")["errors"])
    assert validate_registry_value(network, "yes")["errors"] == [
        "sandbox.bash_allow_network 必须是 true/false。"
    ]
    assert validate_registry_value(delays, "1,2,5")["errors"] == []
    assert validate_registry_value(delays, "1,no")["errors"] == [
        "gateway.platform_reconnect_delays 必须只包含整数。"
    ]


def test_config_registry_schema_is_stable():
    from personal_agent.config_registry import registry_schema

    schema = registry_schema()
    fields = {item["path"]: item for item in schema["fields"]}

    assert schema["version"] == 1
    assert schema["field_count"] == len(schema["fields"])
    assert "execution" in schema["sections"]
    assert fields["LLM_API_KEY"]["sensitive"] is True
    assert fields["LLM_REASONING_EFFORT"]["value_type"] == "str"
    assert fields["LLM_REASONING_EFFORT"]["default"] == ""
    assert fields["llm.context_window"]["env_key"] == "LLM_CONTEXT_WINDOW"
    assert fields["llm.context_window"]["yaml_path"] == "llm.context_window"
    assert fields["llm.context_window"]["minimum"] == 0
    assert fields["attachments.resolve_inbound"]["value_type"] == "bool"
    assert fields["multimodal.text_extract_max_chars"]["value_type"] == "int"
    assert fields["multimodal.text_extract_pdf_max_pages"]["minimum"] == 1
    assert fields["multimodal.image_text_mode"]["choices"] == ["auto", "vision", "ocr", "off"]
    assert fields["multimodal.image_text_cache"]["value_type"] == "bool"
    assert fields["multimodal.image_text_provider"]["choices"][0] == ""
    assert set(fields["multimodal.image_text_provider"]["choices"]) == {"", "deepseek", "openai", "anthropic", "openrouter", "xai"}
    assert fields["multimodal.image_text_api_mode"]["choices"] == [
        "anthropic_messages",
        "auto",
        "chat_completions",
        "codex_responses",
        "responses",
    ]
    assert fields["IMAGE_TEXT_API_KEY"]["sensitive"] is True
    assert fields["multimodal.ocr_timeout_seconds"]["minimum"] == 1
    assert fields["multimodal.ocr_language"]["value_type"] == "str"
    assert fields["profiles"]["env_key"] == "PROFILES"
    assert fields["profiles"]["yaml_path"] == "profiles"
    assert fields["execution.mode"]["choices"] == ["guarded", "standard", "trusted", "sovereign"]
    assert fields["LLM_API_MODE"]["choices"] == [
        "anthropic_messages",
        "auto",
        "chat_completions",
        "codex_responses",
        "responses",
    ]


def test_config_registry_attrs_exist_on_settings(tmp_path):
    from personal_agent.config import Settings
    from personal_agent.config_registry import CONFIG_FIELDS

    settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])

    missing = [field.attr for field in CONFIG_FIELDS if not hasattr(settings, field.attr)]

    assert missing == []


def test_effective_config_snapshot_masks_sensitive_values(tmp_path):
    from personal_agent.config import Settings
    from personal_agent.config_registry import CONFIG_REGISTRY, effective_config_snapshot

    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[],
        llm_api_key="secret-key",
        telegram_bot_token="telegram-secret",
    )

    snapshot = effective_config_snapshot(settings)
    fields = {item["path"]: item for item in snapshot["fields"]}

    assert snapshot["field_count"] == len(snapshot["fields"])
    assert fields["LLM_API_KEY"]["value"] == "<set>"
    assert fields["LLM_API_KEY"]["is_set"] is True
    assert "secret-key" not in str(snapshot["values"])
    assert "secret-key" not in str(snapshot["fields"])
    assert fields["TELEGRAM_BOT_TOKEN"]["value"] == "<set>"
    assert fields["storage.data_dir"]["value"].endswith("data")
    assert isinstance(fields["plugins.dirs"]["value"], list)
    assert snapshot["values"]["LLM_API_KEY"] == "<set>"
    assert snapshot["attr_values"]["llm_api_key"] == "<set>"
    assert snapshot["sources"]["LLM_API_KEY"] == ".env"
    assert snapshot["source_counts"][".env"] > 0
    assert "llm" in snapshot["sections"]

    typed_snapshot = CONFIG_REGISTRY.snapshot_from_settings(settings)
    assert typed_snapshot.field_count == snapshot["field_count"]
    assert typed_snapshot.values["LLM_API_KEY"] == "<set>"
    assert typed_snapshot.attr_values["llm_api_key"] == "secret-key"
