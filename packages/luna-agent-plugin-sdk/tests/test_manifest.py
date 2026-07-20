from __future__ import annotations

import pytest

from luna_agent_plugin_sdk import PluginDependencies, PluginManifest


def test_manifest_parses_dependency_contracts() -> None:
    manifest = PluginManifest.from_mapping({
        "schema_version": 1,
        "plugin_api": ">=1,<2",
        "key": "integrations/report",
        "name": "Report",
        "version": "0.1.0",
        "entrypoint": "report:register",
        "requires": {
            "luna_agent": ">=0.1",
            "sdk": ">=0.1,<0.2",
            "plugins": [{"key": "integrations/github", "version": ">=0.3"}],
            "capabilities": ["conversation.submit"],
            "mcp_tools": {"github": ["list_pull_requests"]},
        },
    })

    assert manifest.plugin_api == ">=1,<2"
    assert manifest.requires.luna_agent == ">=0.1"
    assert manifest.requires.plugins[0].key == "integrations/github"
    assert manifest.requires.capabilities == ("conversation.submit",)
    assert manifest.requires.mcp_tools["github"] == ("list_pull_requests",)


def test_manifest_accepts_legacy_host_dependency_name() -> None:
    manifest = PluginManifest.from_mapping({
        "key": "integrations/legacy",
        "name": "Legacy",
        "version": "0.1.0",
        "entrypoint": "legacy:register",
        "requires": {"lumora": ">=0.1"},
    })

    assert manifest.requires.luna_agent == ">=0.1"
    assert manifest.requires.lumora == ">=0.1"
    assert manifest.requires.as_dict()["luna_agent"] == ">=0.1"
    assert "lumora" not in manifest.requires.as_dict()


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


def test_manifest_normalizes_python_dependencies() -> None:
    dependencies = PluginDependencies.from_mapping({
        "python": [
            "Markdown-It-Py==4.2.0",
            "linkify-it-py>=2; python_version >= '3.12'",
        ],
    })

    assert dependencies.python == (
        'linkify-it-py>=2; python_version >= "3.12"',
        "Markdown-It-Py==4.2.0",
    )
    assert dependencies.as_dict()["python"] == list(dependencies.python)


@pytest.mark.parametrize(
    "value",
    [
        "-r requirements.txt",
        "git+https://example.com/x",
        "demo @ https://example.com/demo.whl",
    ],
)
def test_manifest_rejects_unsafe_python_dependencies(value: str) -> None:
    with pytest.raises(ValueError):
        PluginDependencies.from_mapping({"python": [value]})
