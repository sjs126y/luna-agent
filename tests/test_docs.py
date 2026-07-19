"""Documentation smoke tests."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_readme_links_to_existing_docs():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for path in [
        "docs/configuration.md",
        "docs/platforms.md",
        "docs/plugins.md",
        "docs/operations.md",
    ]:
        assert f"]({path})" in readme
        assert (ROOT / path).exists()


def test_readme_uses_current_cli_commands():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "uv run luna-agent chat" in readme
    assert "uv run luna-agent serve" in readme
    assert "uv run luna-agent doctor" in readme
    assert "python -m luna_agent --cli" not in readme
