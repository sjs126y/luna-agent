"""Configuration registry behavior."""

from __future__ import annotations


def test_config_registry_paths_are_unique():
    from personal_agent.config_registry import CONFIG_FIELDS

    paths = [field.path for field in CONFIG_FIELDS]

    assert len(paths) == len(set(paths))
    assert "execution.mode" in paths
    assert "gateway.platform_send_max_retries" in paths


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
