"""Unit tests for InlineTuiApp wiring (no real terminal).

Covers completer/history wiring, Ctrl+O expand behavior, and the guard that
prevents launching a second turn while one is running.
"""

from __future__ import annotations

import asyncio

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

    mode = "normal"

    async def current_execution_mode(self) -> str:
        return self.mode


def _app() -> InlineTuiApp:
    return InlineTuiApp(_Runtime())


def test_completer_and_history_wired():
    app = _app()
    assert app.input_area.completer is not None
    assert app.input_area.buffer.history is not None


def test_expand_last_noop_when_empty():
    app = _app()
    printed: list[str] = []
    app._print_above_nowait = printed.append  # type: ignore[method-assign]
    app._expand_last()
    assert printed == []


def test_expand_last_prints_when_present():
    app = _app()
    printed: list[str] = []
    app._print_above_nowait = printed.append  # type: ignore[method-assign]
    app.state.last_expandable = ("read", "line1\nline2")
    app._expand_last()
    assert len(printed) == 1
    assert "read" in printed[0]
    assert "line1" in printed[0] and "line2" in printed[0]


def test_expand_last_dedups_repeated_presses():
    app = _app()
    printed: list[str] = []
    app._print_above_nowait = printed.append  # type: ignore[method-assign]
    app.state.last_expandable = ("read", "data")
    app._expand_last()
    app._expand_last()
    app._expand_last()
    assert len(printed) == 1  # same content only printed once


@pytest.mark.asyncio
async def test_print_above_serializes_concurrent_callers(monkeypatch):
    # Reproduces the parallel-tools race: several callers enqueue lines at the
    # same time. One print worker must flush them without overlap or loss.
    app = _app()
    active = {"n": 0, "max": 0}
    done: list[int] = []
    written: list[str] = []

    async def fake_run_in_terminal(func):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        await asyncio.sleep(0)  # yield: overlap would show up here
        func()
        active["n"] -= 1

    monkeypatch.setattr("personal_agent.tui.app.run_in_terminal", fake_run_in_terminal)
    app._write_terminal = written.append  # type: ignore[method-assign]

    async def one(i: int) -> None:
        await app._print_above(str(i))
        done.append(i)

    await asyncio.gather(*(one(i) for i in range(5)))
    await app._stop_print_worker()
    assert active["max"] == 1  # never more than one in the terminal at once
    assert len(done) == 5  # none dropped
    text = "".join(written)
    for i in range(5):
        assert f"{i}\n" in text


@pytest.mark.asyncio
async def test_print_above_nowait_uses_same_queue(monkeypatch):
    app = _app()
    written: list[str] = []

    async def fake_run_in_terminal(func):
        func()

    monkeypatch.setattr("personal_agent.tui.app.run_in_terminal", fake_run_in_terminal)
    app._write_terminal = written.append  # type: ignore[method-assign]

    app._print_above_nowait("expand")
    await app._stop_print_worker()
    assert written == ["expand\n"]


@pytest.mark.asyncio
async def test_submit_routes_message_to_runtime():
    app = _app()
    printed: list[str] = []

    async def print_above(text):
        printed.append(text)

    app._print_above = print_above  # type: ignore[method-assign]
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
async def test_refresh_mode_updates_status_after_command():
    app = _app()

    async def print_above(text):
        pass

    app._print_above = print_above  # type: ignore[method-assign]

    async def handle_command(text):
        app.runtime.mode = "auto"
        return "执行模式已切换: auto"

    app.runtime.handle_command = handle_command  # type: ignore[method-assign]
    assert app.state.exec_mode == "normal"
    await app._submit("/mode auto")
    assert app.state.exec_mode == "auto"


@pytest.mark.asyncio
async def test_cycle_mode_advances_and_wraps():
    app = _app()
    printed: list[str] = []

    async def print_above(text):
        printed.append(text)

    app._print_above = print_above  # type: ignore[method-assign]

    async def handle_command(text):
        # emulate backend: /mode X sets the runtime's reported mode
        app.runtime.mode = text.split()[1]
        return f"执行模式已切换: {app.runtime.mode}"

    app.runtime.handle_command = handle_command  # type: ignore[method-assign]

    assert app.state.exec_mode == "normal"
    await app._cycle_mode()
    assert app.state.exec_mode == "acceptEdits"
    await app._cycle_mode()
    assert app.state.exec_mode == "auto"
    await app._cycle_mode()
    assert app.state.exec_mode == "normal"  # wraps


@pytest.mark.asyncio
async def test_command_not_sent_as_message():
    app = _app()

    async def handle_command(text):
        return "command output"

    app.runtime.handle_command = handle_command  # type: ignore[method-assign]
    printed: list[str] = []

    async def print_above(text):
        printed.append(text)

    app._print_above = print_above  # type: ignore[method-assign]
    await app._submit("/help")
    assert app.runtime.sent == []  # not routed as a message
    assert any("command output" in line for line in printed)
