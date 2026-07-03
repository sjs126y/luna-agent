"""Terminal shell behavior without starting the full app runtime."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from personal_agent.cli_shell import CliShell, ShellRenderOptions, TerminalRenderer
from personal_agent.conversation.events import emit_event


class FakeRuntime:
    session_key = "cli:default:local"
    settings = SimpleNamespace(llm_provider="deepseek", llm_model="deepseek-chat")

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.messages: list[str] = []

    async def handle_command(self, text: str):
        self.commands.append(text)
        if text == "/help":
            return "help text"
        return None

    async def run_message_events(self, text: str, *, event_sink=None):
        self.messages.append(text)
        await emit_event(event_sink, "llm_start", "请求模型")
        await emit_event(event_sink, "assistant_message", f"echo:{text}")
        await emit_event(event_sink, "turn_end", "完成")
        return SimpleNamespace(final_response=f"echo:{text}")


@pytest.mark.asyncio
async def test_cli_shell_handles_command_without_running_turn():
    output: list[str] = []
    runtime = FakeRuntime()
    renderer = TerminalRenderer(
        output_fn=output.append,
        options=ShellRenderOptions(show_events=True),
    )
    shell = CliShell(runtime, renderer=renderer)

    result = await shell.run_once("/help")

    assert result == "help text"
    assert output == ["help text"]
    assert runtime.messages == []


@pytest.mark.asyncio
async def test_cli_shell_renders_message_events():
    output: list[str] = []
    runtime = FakeRuntime()
    renderer = TerminalRenderer(
        output_fn=output.append,
        options=ShellRenderOptions(show_events=True),
    )
    shell = CliShell(runtime, renderer=renderer)

    result = await shell.run_once("你好")

    assert result == "echo:你好"
    assert runtime.messages == ["你好"]
    assert "[模型] 请求中" in output
    assert "\nassistant:\necho:你好" in output
