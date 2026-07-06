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

    mode = "Ask First"

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
    assert "展开 read" in printed[0]
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
async def test_submit_echoes_user_message_with_background():
    app = _app()
    printed: list[str] = []

    async def print_above(text):
        printed.append(text)

    app._print_above = print_above  # type: ignore[method-assign]
    await app._submit("现在试一下？")
    text = "\n".join(printed)
    assert "现在试一下？" in text
    assert "\x1b[1;37;48;5;236m" in text


@pytest.mark.asyncio
async def test_submit_preserves_message_body_whitespace():
    app = _app()

    async def print_above(text):
        pass

    app._print_above = print_above  # type: ignore[method-assign]
    await app._submit("  keep\n  indentation  ")
    assert app.runtime.sent == ["  keep\n  indentation  "]
    assert app.runtime.commands == ["keep\n  indentation"]


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
        app.runtime.mode = "Full Auto"
        return "执行模式已切换: Full Auto"

    app.runtime.handle_command = handle_command  # type: ignore[method-assign]
    assert app.state.exec_mode == "Ask First"
    await app._submit("/mode Full Auto")
    assert app.state.exec_mode == "Full Auto"


@pytest.mark.asyncio
async def test_cycle_mode_advances_and_wraps():
    app = _app()
    printed: list[str] = []

    async def print_above(text):
        printed.append(text)

    app._print_above = print_above  # type: ignore[method-assign]

    async def handle_command(text):
        # emulate backend: /mode X sets the runtime's reported mode
        app.runtime.mode = text.removeprefix("/mode ").strip()
        return f"执行模式已切换: {app.runtime.mode}"

    app.runtime.handle_command = handle_command  # type: ignore[method-assign]

    assert app.state.exec_mode == "Ask First"
    await app._cycle_mode()
    assert app.state.exec_mode == "Edit Freely"
    await app._cycle_mode()
    assert app.state.exec_mode == "Full Auto"
    await app._cycle_mode()
    assert app.state.exec_mode == "Read Only"
    await app._cycle_mode()
    assert app.state.exec_mode == "Ask First"  # wraps


@pytest.mark.asyncio
async def test_confirm_tool_resolves_allow():
    app = _app()
    task = asyncio.ensure_future(app.confirm_tool({
        "tool_name": "write_file",
        "permission_category": "write",
        "risk_summary": "将写入文件",
        "input_preview": "src/app.py",
    }))
    await asyncio.sleep(0)  # let confirm_tool set pending + create the future
    assert app.state.pending_confirm is not None
    assert app.state.pending_confirm.display_name == "write_file"
    assert app.state.pending_confirm.risk_summary == "将写入文件"
    assert app.state.pending_confirm.input_preview == "src/app.py"
    app.input_area.text = "y"
    app._resolve_confirm("allow")
    assert await task == "allow"
    # cleaned up after resolution
    assert app.state.pending_confirm is None
    assert app._confirm_future is None
    assert app.input_area.text == ""


@pytest.mark.asyncio
async def test_confirm_tool_resolves_deny_and_always():
    app = _app()
    task = asyncio.ensure_future(app.confirm_tool({"tool_name": "bash"}))
    await asyncio.sleep(0)
    app._resolve_confirm("deny")
    assert await task == "deny"

    task = asyncio.ensure_future(app.confirm_tool({"tool_name": "bash"}))
    await asyncio.sleep(0)
    app._resolve_confirm("always")
    assert await task == "always"


def test_confirm_prompt_uses_future_display_fields():
    app = _app()
    prompt = app._build_confirm_prompt({
        "display_name": "Run shell command",
        "tool_name": "bash",
        "permission_category": "bash",
        "execution_mode_label": "Ask First",
        "default_action": "deny",
        "available_actions": ["deny"],
        "command_preview": "rm -rf build",
        "affected_paths": ["build"],
    })
    assert prompt.display_name == "Run shell command"
    assert prompt.permission_category == "bash"
    assert prompt.execution_mode == "Ask First"
    assert prompt.default_action == "deny"
    assert prompt.available_actions == ("deny",)
    assert "rm -rf build" in prompt.input_preview
    assert prompt.command_preview == "rm -rf build"
    assert prompt.affected_paths == ("build",)
    assert "build" in prompt.input_preview


def test_confirm_prompt_preserves_network_display_fields():
    app = _app()
    prompt = app._build_confirm_prompt({
        "display_name": "Fetch URL",
        "tool_name": "web_fetch",
        "url_preview": "https://example.test/a",
        "host": "example.test",
        "input_preview": "https://example.test/a",
    })
    assert prompt.url_preview == "https://example.test/a"
    assert prompt.host == "example.test"


@pytest.mark.asyncio
async def test_confirm_action_respects_available_actions():
    app = _app()
    fut = asyncio.get_running_loop().create_future()
    app._confirm_future = fut
    app.state.pending_confirm = app._build_confirm_prompt({
        "tool_name": "bash",
        "available_actions": ["deny"],
    })
    app._resolve_confirm_action("allow_once")
    assert fut.done() is False
    app._resolve_confirm("deny")


def test_runtime_accepts_confirm_detection():
    app = _app()
    # fake runtime's run_message_events is (text, event_sink=None): no confirm
    assert app._runtime_accepts_confirm() is False

    async def with_confirm(text, event_sink=None, confirm=None):
        return None

    app.runtime.run_message_events = with_confirm  # type: ignore[method-assign]
    assert app._runtime_accepts_confirm() is True

    async def with_kwargs(text, event_sink=None, **kw):
        return None

    app.runtime.run_message_events = with_kwargs  # type: ignore[method-assign]
    assert app._runtime_accepts_confirm() is True


def test_enter_keeps_draft_while_turn_running():
    app = _app()
    class _RunningTask:
        def done(self) -> bool:
            return False

    app._turn_task = _RunningTask()  # type: ignore[assignment]
    app.input_area.text = "next question"
    app._on_enter()
    assert app.input_area.text == "next question"


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
