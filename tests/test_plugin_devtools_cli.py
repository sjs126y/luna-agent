from __future__ import annotations

import json
from pathlib import Path
import zipfile

from typer.testing import CliRunner

from personal_agent.cli import app


runner = CliRunner()


def test_plugin_ai_cli_create_test_and_package(tmp_path):
    spec = tmp_path / "spec.json"
    root = tmp_path / "plugin"
    archive = tmp_path / "plugin.zip"
    spec.write_text(json.dumps({
        "key": "tests/cli-generated",
        "name": "CLI Generated",
        "features": ["hook"],
        "hook_events": ["PreToolUse"],
    }), encoding="utf-8")

    created = runner.invoke(app, [
        "plugins", "create", str(root), "--spec", str(spec), "--json",
    ])
    checked = runner.invoke(app, [
        "plugins", "test", str(root), "--contract", "--integration", "--json",
    ])
    packaged = runner.invoke(app, [
        "plugins", "package", str(root), "--output", str(archive),
    ])

    assert created.exit_code == 0, created.output
    assert json.loads(created.output)["ok"] is True
    assert checked.exit_code == 0, checked.output
    report = json.loads(checked.output)
    assert report["ok"] is True
    assert {item["mode"] for item in report["checks"]} == {"contract", "integration"}
    assert packaged.exit_code == 0, packaged.output
    with zipfile.ZipFile(archive) as package:
        assert "plugin.yaml" in package.namelist()
        assert "AGENTS.md" in package.namelist()


def test_plugin_ai_cli_exposes_machine_readable_capabilities_and_schema():
    capabilities = runner.invoke(app, ["plugins", "capabilities", "--json"])
    schema = runner.invoke(app, ["plugins", "schema", "scaffold"])

    assert capabilities.exit_code == 0
    assert "hook" in json.loads(capabilities.output)["registration"]
    assert schema.exit_code == 0
    assert "hook_events" in json.loads(schema.output)["properties"]
