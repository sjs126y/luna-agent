from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_list_directory_returns_only_immediate_entries(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.file_navigation import _list_directory
    from personal_agent.tools.sandbox import init_sandbox

    (tmp_path / "alpha.txt").write_text("alpha", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "child.txt").write_text("child", encoding="utf-8")
    init_sandbox([tmp_path], [])

    payload = json.loads(await _list_directory(str(tmp_path)))

    assert [entry["name"] for entry in payload["entries"]] == ["alpha.txt", "nested"]
    assert payload["entries"][0]["type"] == "file"
    assert payload["entries"][0]["size_bytes"] == 5
    assert payload["entries"][1]["type"] == "directory"
    assert "child.txt" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_list_directory_paginates_sorted_entries(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.file_navigation import _list_directory
    from personal_agent.tools.sandbox import init_sandbox

    for name in ["charlie.txt", "alpha.txt", "bravo.txt"]:
        (tmp_path / name).write_text(name, encoding="utf-8")
    init_sandbox([tmp_path], [])

    first = json.loads(await _list_directory(str(tmp_path), limit=2))
    second = json.loads(await _list_directory(str(tmp_path), offset=2, limit=2))

    assert [entry["name"] for entry in first["entries"]] == ["alpha.txt", "bravo.txt"]
    assert first["next_offset"] == 2
    assert first["truncated"] is True
    assert [entry["name"] for entry in second["entries"]] == ["charlie.txt"]
    assert second["next_offset"] is None


@pytest.mark.asyncio
async def test_list_directory_hides_hidden_and_blocked_entries(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.file_navigation import _list_directory
    from personal_agent.tools.sandbox import init_sandbox

    (tmp_path / ".notes.txt").write_text("hidden", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("protected", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("visible", encoding="utf-8")
    init_sandbox([tmp_path], ["**/pyproject.toml"])

    default = json.loads(await _list_directory(str(tmp_path)))
    hidden = json.loads(await _list_directory(str(tmp_path), include_hidden=True))

    assert [entry["name"] for entry in default["entries"]] == ["visible.txt"]
    assert [entry["name"] for entry in hidden["entries"]] == [".notes.txt", "visible.txt"]


@pytest.mark.asyncio
async def test_list_directory_rejects_blocked_directory(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.file_navigation import _list_directory
    from personal_agent.tools.sandbox import init_sandbox

    protected = tmp_path / ".ssh"
    protected.mkdir()
    init_sandbox([tmp_path], ["**/.ssh"])

    result = await _list_directory(str(protected))

    assert "path blocked by sandbox" in result


@pytest.mark.asyncio
async def test_file_info_reports_text_metadata_and_write_scope(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.file_navigation import _file_info
    from personal_agent.tools.sandbox import init_sandbox

    workspace = tmp_path / "workspace"
    readonly = tmp_path / "readonly"
    workspace.mkdir()
    readonly.mkdir()
    target = readonly / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    init_sandbox([workspace], [], read_roots=[readonly])

    payload = json.loads(await _file_info(str(target)))

    assert payload["name"] == "notes.txt"
    assert payload["type"] == "file"
    assert payload["size_bytes"] == 5
    assert payload["mime_type"] == "text/plain"
    assert payload["content_kind"] == "text"
    assert payload["readable"] is True
    assert payload["writable"] is False


@pytest.mark.asyncio
async def test_file_info_detects_binary_and_rejects_blocked_path(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.file_navigation import _file_info
    from personal_agent.tools.sandbox import init_sandbox

    binary = tmp_path / "payload.bin"
    binary.write_bytes(b"\x00\x01\x02")
    protected = tmp_path / "pyproject.toml"
    protected.write_text("secret", encoding="utf-8")
    init_sandbox([tmp_path], ["**/pyproject.toml"])

    payload = json.loads(await _file_info(str(binary)))
    denied = await _file_info(str(protected))

    assert payload["content_kind"] == "binary"
    assert "path blocked by sandbox" in denied


def test_navigation_tools_are_registered_as_read_only_core_tools():
    import personal_agent.plugins.builtin.tools.builtin.file_navigation  # noqa: F401
    from personal_agent.tools.registry import tool_registry
    from personal_agent.tools.toolsets import TOOLSETS, is_core_tool

    for name in {"list_directory", "file_info"}:
        entry = tool_registry.get(name)
        assert entry is not None
        assert entry.permission_category == "read"
        assert entry.risk_level == "low"
        assert is_core_tool(name)
        assert name in TOOLSETS["file"]
