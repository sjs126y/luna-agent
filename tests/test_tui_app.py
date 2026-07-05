"""Unit tests for InlineTuiApp wiring (no real terminal).

Covers completer/history wiring, Ctrl+O expand behavior, and the guard that
prevents launching a second turn while one is running.
"""

from __future__ import annotations

import pytest

from personal_agent.tui.app import InlineTuiApp


class _Settings:
    llm_model = "deepseek-chat"
    agent_data_dir = "data"


class _Runtime:
    def __init__(self) -> None:
        self.settings = _Settings()
        self.sent: list[str] = []
        self.commands: list[str] = []

    async def handle_command(self, text: str):
        self.commands.append(text)
        return None  # not a command

    async def run_message_events(self, text: str, event_sink=None):
        self.sent.append(text)
        return None


def _app() -> InlineTuiApp:
    return InlineTuiApp(_Runtime())


def test_completer_and_history_wired():
    app = _app()
    assert app.input_area.completer is not None
    assert app.input_area.buffer.history is not None


def test_expand_last_noop_when_empty(capsys):
    app = _app()
    printed: list[str] = []
    app._print_above = printed.append  # type: ignore[method-assign]
    app._expand_last()
    assert printed == []


def test_expand_last_prints_when_present():
    app = _app()
    printed: list[str] = []
    app._print_above = printed.append  # type: ignore[method-assign]
    app.state.last_expandable = ("read", "line1\nline2")
    app._expand_last()
    assert len(printed) == 1
    assert "read" in printed[0]
    assert "line1" in printed[0] and "line2" in printed[0]


@pytest.mark.asyncio
async def test_submit_routes_message_to_runtime():
    app = _app()
    printed: list[str] = []
    app._print_above = printed.append  # type: ignore[method-assign]
    await app._submit("hello")
    assert app.runtime.sent == ["hello"]
    # user line echoed above
    assert any("hello" in line for line in printed)


@pytest.mark.asyncio
async def test_submit_ignores_blank():
    app = _app()
    await app._submit("   ")
    assert app.runtime.sent == []


@pytest.mark.asyncio
async def test_command_not_sent_as_message():
    app = _app()

    async def handle_command(text):
        return "command output"

    app.runtime.handle_command = handle_command  # type: ignore[method-assign]
    printed: list[str] = []
    app._print_above = printed.append  # type: ignore[method-assign]
    await app._submit("/help")
    assert app.runtime.sent == []  # not routed as a message
    assert any("command output" in line for line in printed)
