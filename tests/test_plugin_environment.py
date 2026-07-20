from __future__ import annotations

import json
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


def test_environment_identity_changes_with_sdk_version(tmp_path: Path, monkeypatch) -> None:
    import luna_agent.plugins.install.environment as environment_module

    manager = PluginEnvironmentManager(tmp_path)
    current = manager.environment_id("external/demo", [])
    monkeypatch.setattr(environment_module, "SDK_VERSION", "999.0.0")

    assert manager.environment_id("external/demo", []) != current


def test_environment_gc_is_dry_run_by_default_and_preserves_references(tmp_path: Path) -> None:
    manager = PluginEnvironmentManager(tmp_path)
    retained = manager.inspect("external/keep", [])
    removable = manager.inspect("external/remove", ["demo==1"])
    for environment in (retained, removable):
        environment.root.mkdir(parents=True)
        (environment.root / "environment.json").write_text(
            json.dumps(environment.as_dict()),
            encoding="utf-8",
        )
        environment.python.parent.mkdir(parents=True, exist_ok=True)
        environment.python.write_text("", encoding="utf-8")

    preview = manager.collect_garbage(
        retained={(retained.plugin_key, retained.environment_id): ["runtime_generation"]},
    )
    assert preview["dry_run"] is True
    assert preview["retained"][0]["environment_id"] == retained.environment_id
    assert preview["removable"][0]["environment_id"] == removable.environment_id
    assert removable.root.exists()

    applied = manager.collect_garbage(
        retained={(retained.plugin_key, retained.environment_id): ["runtime_generation"]},
        dry_run=False,
    )
    assert applied["removed"][0]["environment_id"] == removable.environment_id
    assert retained.root.exists()
    assert not removable.root.exists()


def test_environment_gc_conservatively_keeps_invalid_metadata_and_plugin_keys(
    tmp_path: Path,
) -> None:
    manager = PluginEnvironmentManager(tmp_path)
    invalid = tmp_path / "invalid__plugin" / "broken"
    invalid.mkdir(parents=True)
    (invalid / "environment.json").write_text("not json", encoding="utf-8")
    known = manager.inspect("external/known", [])
    known.root.mkdir(parents=True)
    (known.root / "environment.json").write_text(
        json.dumps(known.as_dict()),
        encoding="utf-8",
    )

    report = manager.collect_garbage(
        retained={},
        retain_plugin_keys={"external/known"},
        dry_run=False,
    )
    assert {item["reasons"][0] for item in report["retained"]} == {
        "invalid_metadata",
        "installed_manifest_unavailable",
    }
    assert invalid.exists() and known.root.exists()


def test_environment_gc_preserves_a_process_lease(tmp_path: Path) -> None:
    manager = PluginEnvironmentManager(tmp_path)
    environment = manager.inspect("external/live", [])
    environment.root.mkdir(parents=True)
    (environment.root / "environment.json").write_text(
        json.dumps(environment.as_dict()),
        encoding="utf-8",
    )
    lease = manager.acquire_lease(
        environment.plugin_key,
        environment.environment_id,
        "external-live:runtime",
    )
    try:
        report = manager.collect_garbage(retained={}, dry_run=False)
        assert report["removable"] == []
        assert report["retained"][0]["reasons"] == ["active_process_lease"]
        assert environment.root.exists()
    finally:
        lease.close()
