"""Configuration registry behavior."""

from __future__ import annotations


def test_config_registry_paths_are_unique():
    from personal_agent.config_registry import CONFIG_FIELDS

    paths = [field.path for field in CONFIG_FIELDS]

    assert len(paths) == len(set(paths))
    assert "execution.mode" in paths
    assert "gateway.platform_send_max_retries" in paths


def test_config_registry_exposes_known_yaml_sections_and_keys():
    from personal_agent.config_registry import (
        config_yaml_known_keys_by_section,
        config_yaml_known_sections,
    )

    sections = config_yaml_known_sections()
    keys = config_yaml_known_keys_by_section()

    assert "execution" in sections
    assert "gateway" in sections
    assert "profiles" in sections
    assert "platform_send_max_retries" in keys["gateway"]
    assert "embedding" in keys["memory"]
    assert keys["profiles"] is None


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


def test_config_registry_attrs_exist_on_settings(tmp_path):
    from personal_agent.config import Settings
    from personal_agent.config_registry import CONFIG_FIELDS

    settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])

    missing = [field.attr for field in CONFIG_FIELDS if not hasattr(settings, field.attr)]

    assert missing == []


def test_effective_config_snapshot_masks_sensitive_values(tmp_path):
    from personal_agent.config import Settings
    from personal_agent.config_registry import effective_config_snapshot

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
    assert "secret-key" not in str(snapshot)
    assert fields["TELEGRAM_BOT_TOKEN"]["value"] == "<set>"
    assert fields["storage.data_dir"]["value"].endswith("data")
    assert isinstance(fields["plugins.dirs"]["value"], list)
