from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

from personal_agent.plugins.devtools import (
    capability_catalog,
    contract_test,
    create_plugin,
    diff_plugins,
    package_plugin,
    schema_document,
)


def _spec(**changes):
    value = {
        "key": "examples/generated",
        "name": "Generated Plugin",
        "version": "0.1.0",
        "description": "Generated during tests.",
        "features": ["active", "command", "hook", "mcp", "skill"],
        "hook_events": ["PreToolUse", "PostDelivery"],
    }
    value.update(changes)
    return value


def test_create_plugin_generates_ai_contract_and_all_extension_shapes(tmp_path):
    root = tmp_path / "generated"

    paths = create_plugin(root, _spec())
    result = contract_test(root)

    assert root / "AGENTS.md" in paths
    assert (root / "tests" / "test_contract.py").is_file()
    assert (root / "skills" / "example" / "SKILL.md").is_file()
    assert (root / "mcp.yaml").is_file()
    assert result["ok"] is True
    assert result["registrations"]["active"] == 1
    assert result["registrations"]["commands"] == 1
    assert result["registrations"]["hooks"] == 2
    assert result["registrations"]["mcp_files"] == ["mcp.yaml"]
    assert result["registrations"]["skill_directories"] == ["skills"]


def test_plugin_package_is_deterministic_and_excludes_runtime_cache(tmp_path):
    root = tmp_path / "generated"
    create_plugin(root, _spec(features=["hook"], hook_events=["PreToolUse"]))
    cache = root / "__pycache__" / "generated.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"cache")
    first = package_plugin(root, tmp_path / "first.zip")
    second = package_plugin(root, tmp_path / "second.zip")

    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    with zipfile.ZipFile(first) as archive:
        assert "plugin.yaml" in archive.namelist()
        assert not any("__pycache__" in name for name in archive.namelist())


def test_plugin_diff_reports_contract_and_file_changes(tmp_path):
    before = tmp_path / "before"
    after = tmp_path / "after"
    create_plugin(before, _spec(features=[], hook_events=[]))
    create_plugin(after, _spec(version="0.2.0", features=["hook"], hook_events=["PreToolUse"]))

    report = diff_plugins(before, after)

    assert report["compatible_key"] is True
    assert report["version"] == {"before": "0.1.0", "after": "0.2.0"}
    assert "hook" in report["manifest"]["provides_added"]
    assert "generated.py" in report["files"]["changed"]


def test_plugin_machine_schemas_include_hooks_dependencies_and_resources():
    manifest = schema_document("manifest")
    scaffold = schema_document("scaffold")
    catalog = capability_catalog()

    assert "requires" in manifest["properties"]
    assert "hook_events" in scaffold["properties"]
    assert "conversation.submit" in catalog["host_resources"]
    assert "PreToolUse" in catalog["hook_events"]
    json.dumps(manifest)
