from __future__ import annotations

from pathlib import Path

from luna_agent.plugins.install.environment import PluginEnvironmentManager


def test_environment_identity_is_per_plugin_and_dependency_set(tmp_path: Path) -> None:
    manager = PluginEnvironmentManager(tmp_path)
    first = manager.inspect("external/one", ["demo==1", "other>=2"])
    same = manager.inspect("external/one", ["other>=2", "demo==1"])
    other_plugin = manager.inspect("external/two", ["demo==1", "other>=2"])
    changed = manager.inspect("external/one", ["demo==2", "other>=2"])

    assert first.environment_id == same.environment_id
    assert first.environment_id != other_plugin.environment_id
    assert first.environment_id != changed.environment_id
    assert first.status == "missing"


def test_ready_environment_requires_metadata_and_python(tmp_path: Path) -> None:
    manager = PluginEnvironmentManager(tmp_path)
    expected = manager.inspect("external/demo", [])
    expected.root.mkdir(parents=True)
    (expected.root / "environment.json").write_text("{}", encoding="utf-8")

    assert manager.inspect("external/demo", []).status == "missing"

    expected.python.parent.mkdir(parents=True, exist_ok=True)
    expected.python.write_text("", encoding="utf-8")
    assert manager.inspect("external/demo", []).status == "ready"
