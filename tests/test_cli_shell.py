"""Terminal shell behavior without starting the full app runtime."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from personal_agent.cli_shell import CliShell, ShellRenderOptions, TerminalRenderer
from personal_agent.conversation.events import ConversationEvent, emit_event


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
        await emit_event(
            event_sink,
            "llm_end",
            "模型返回",
            input_tokens=10,
            output_tokens=3,
        )
        await emit_event(event_sink, "turn_end", "完成")
        return SimpleNamespace(final_response=f"echo:{text}", raw={"api_calls": 1})


def _renderer(options: ShellRenderOptions | None = None):
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None, width=100)
    return TerminalRenderer(console=console, options=options), stream


def test_cli_shell_prompt_renders_status_input_line():
    renderer, stream = _renderer()
    runtime = FakeRuntime()

    prompt = renderer.prompt(runtime)

    text = stream.getvalue()
    assert prompt == "› "
    assert "deepseek-chat" in text
    assert "─" in text


@pytest.mark.asyncio
async def test_cli_shell_handles_command_without_running_turn():
    renderer, stream = _renderer()
    runtime = FakeRuntime()
    shell = CliShell(runtime, renderer=renderer)

    result = await shell.run_once("/help")

    assert result == "help text"
    assert "help text" in stream.getvalue()
    assert "命令" in stream.getvalue()
    assert runtime.messages == []


@pytest.mark.asyncio
async def test_cli_shell_renders_message_events():
    renderer, stream = _renderer()
    runtime = FakeRuntime()
    shell = CliShell(runtime, renderer=renderer)

    result = await shell.run_once("你好")

    assert result == "echo:你好"
    assert runtime.messages == ["你好"]
    text = stream.getvalue()
    assert "你" in text
    assert "你好" in text
    assert "$ PersonalAgent" in text
    assert "│ echo:你好" not in text
    assert "  echo:你好" in text
    assert "╭─ $ PersonalAgent" in text
    assert "╮" in text
    assert "╰" in text
    assert "╯" in text
    assert "echo:你好" in text
    assert "模型:" not in text

    stream.seek(0)
    stream.truncate(0)
    assert renderer.prompt(runtime) == "› "
    prompt_text = stream.getvalue()
    assert "api=1" in prompt_text
    assert "in=10 out=3" in prompt_text


@pytest.mark.asyncio
async def test_cli_shell_verbose_shows_model_lines():
    renderer, stream = _renderer(ShellRenderOptions(verbose=True))
    runtime = FakeRuntime()
    shell = CliShell(runtime, renderer=renderer)

    await shell.run_once("你好")

    text = stream.getvalue()
    assert "模型:" in text
    assert "请求中" in text


@pytest.mark.asyncio
async def test_cli_shell_quiet_events_hides_model_lines():
    renderer, stream = _renderer(ShellRenderOptions(quiet_events=True, show_events=False))
    runtime = FakeRuntime()
    shell = CliShell(runtime, renderer=renderer)

    await shell.run_once("你好")

    text = stream.getvalue()
    assert "echo:你好" in text
    assert "模型:" not in text


@pytest.mark.asyncio
async def test_cli_shell_truncates_tool_summary_by_default():
    renderer, stream = _renderer(ShellRenderOptions(max_tool_summary_chars=30))

    await renderer.emit(
        ConversationEvent(
            type="tool_end",
            data={
                "tool_name": "memory",
                "status": "success",
                "output_summary": "x" * 80,
            },
        )
    )

    text = stream.getvalue()
    assert "memory success" in text
    assert "xxx..." in text
    assert "x" * 80 not in text


@pytest.mark.asyncio
async def test_cli_shell_run_accepts_async_input_function():
    output: list[str] = []
    renderer, stream = _renderer()
    runtime = FakeRuntime()
    inputs = iter(["/help", ""])

    async def input_fn(prompt: str) -> str:
        output.append(prompt)
        return next(inputs)

    shell = CliShell(runtime, input_fn=input_fn, renderer=renderer)

    await shell.run()

    assert "help text" in stream.getvalue()
    assert any(item == "› " for item in output)
    assert runtime.messages == []


def test_cli_shell_sync_loop_handles_input_function():
    import asyncio

    output: list[str] = []
    renderer, stream = _renderer()
    runtime = FakeRuntime()
    inputs = iter(["/help", ""])

    def input_fn(prompt: str) -> str:
        output.append(prompt)
        return next(inputs)

    loop = asyncio.new_event_loop()
    try:
        shell = CliShell(runtime, input_fn=input_fn, renderer=renderer)
        shell.run_sync(loop)
    finally:
        loop.close()

    assert "help text" in stream.getvalue()
    assert any(item == "› " for item in output)
