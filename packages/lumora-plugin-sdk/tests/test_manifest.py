from __future__ import annotations

import pytest

from lumora_plugin_sdk import PluginManifest


def test_manifest_parses_dependency_contracts() -> None:
    manifest = PluginManifest.from_mapping({
        "schema_version": 1,
        "plugin_api": ">=1,<2",
        "key": "integrations/report",
        "name": "Report",
        "version": "0.1.0",
        "entrypoint": "report:register",
        "requires": {
            "lumora": ">=0.1",
            "sdk": ">=0.1,<0.2",
            "plugins": [{"key": "integrations/github", "version": ">=0.3"}],
            "capabilities": ["conversation.submit"],
            "mcp_tools": {"github": ["list_pull_requests"]},
        },
    })

    assert manifest.plugin_api == ">=1,<2"
    assert manifest.requires.plugins[0].key == "integrations/github"
    assert manifest.requires.capabilities == ("conversation.submit",)
    assert manifest.requires.mcp_tools["github"] == ("list_pull_requests",)


@pytest.mark.parametrize("field,value", [
    ("plugin_api", "not-a-range"),
    ("requires", {"sdk": "bad"}),
    ("requires", {"plugins": [{"key": "Bad Key"}]}),
])
def test_manifest_rejects_invalid_dependency_contracts(field, value) -> None:
    data = {
        "key": "integrations/report",
        "name": "Report",
        "version": "0.1.0",
        "entrypoint": "report:register",
        field: value,
    }

    with pytest.raises(ValueError):
        PluginManifest.from_mapping(data)
