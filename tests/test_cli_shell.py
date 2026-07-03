"""Terminal shell behavior without starting the full app runtime."""

from __future__ import annotations

import asyncio
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
        self.stop_calls = 0

    async def handle_command(self, text: str):
        self.commands.append(text)
        if text == "/help":
            return "help text"
        if text == "/stop":
            return "已停止。"
        return None

    async def stop_agents(self):
        self.stop_calls += 1
        return "已请求停止当前处理"

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


class CancellableRuntime(FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.stop_event = asyncio.Event()

    async def run_message_events(self, text: str, *, event_sink=None):
        self.messages.append(text)
        if text == "first":
            self.started.set()
            await self.stop_event.wait()
        await emit_event(event_sink, "assistant_message", f"echo:{text}")
        return SimpleNamespace(final_response=f"echo:{text}", raw={"api_calls": 1})

    async def stop_agents(self):
        self.stop_calls += 1
        self.stop_event.set()
        return "已请求停止当前处理"


def _renderer(options: ShellRenderOptions | None = None):
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None, width=100)
    return TerminalRenderer(console=console, options=options), stream


def _terminal_renderer(options: ShellRenderOptions | None = None):
    stream = StringIO()
    console = Console(file=stream, force_terminal=True, color_system=None, width=100)
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
    assert "$ help text" in stream.getvalue()
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
    assert "$ Personal Agent" in text
    assert "│ echo:你好" not in text
    assert "  echo:你好" in text
    assert "╭─ $ Personal Agent" in text
    assert "╮" in text
    assert "╰" in text
    assert "╯" in text
    assert "echo:你好" in text
    assert "模型:" not in text

    stream.seek(0)
    stream.truncate(0)
    assert renderer.prompt(runtime) == "› "
    prompt_text = stream.getvalue()
    assert "api 1" in prompt_text
    assert "in 10 out 3" in prompt_text
    assert "$ deepseek-chat" in prompt_text


@pytest.mark.asyncio
async def test_cli_shell_run_frames_live_input_area():
    output: list[str] = []
    renderer, stream = _renderer()
    runtime = FakeRuntime()
    inputs = iter(["你好", ""])

    async def input_fn(prompt: str) -> str:
        output.append(prompt)
        return next(inputs)

    shell = CliShell(runtime, input_fn=input_fn, renderer=renderer)

    await shell.run()

    text = stream.getvalue()
    assert "$ deepseek-chat" in text
    assert text.count("─" * 20) >= 2
    assert "╭─ $ Personal Agent" in text
    assert any(item == "› " for item in output)
    assert runtime.messages == ["你好"]


def test_cli_shell_terminal_prompt_avoids_cursor_rewrite_frame():
    renderer, stream = _terminal_renderer(ShellRenderOptions(color=False))
    runtime = FakeRuntime()

    prompt = renderer.prompt(runtime)

    text = stream.getvalue()
    assert prompt == "› "
    assert "› " not in text
    assert "─" * 20 in text
    assert "\x1b[1A" not in text
    assert "\x1b[2C" not in text


@pytest.mark.asyncio
async def test_cli_shell_prompt_toolkit_uses_bottom_toolbar():
    renderer, _ = _terminal_renderer(ShellRenderOptions(color=False))
    runtime = FakeRuntime()
    shell = CliShell(runtime, renderer=renderer)
    calls = []

    class FakePrompt:
        async def prompt_async(self, prompt_text: str, **kwargs):
            calls.append((prompt_text, kwargs))
            return ""

    shell._prompt_session = FakePrompt()

    await shell._read_turn_text()

    assert calls[0][0] == "› "
    toolbar = calls[0][1]["bottom_toolbar"]
    assert "─" * 20 in str(toolbar)
    assert "\x1b[1A" not in str(toolbar)
    assert "\x1b[2C" not in str(toolbar)


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
async def test_cli_shell_renders_tool_trace_without_frames():
    renderer, stream = _renderer(ShellRenderOptions(max_tool_summary_chars=30))

    await renderer.emit(
        ConversationEvent(
            type="tool_start",
            data={
                "tool_name": "web_search",
                "tool_use_id": "t1",
                "input_summary": '{"query": "巧乐兹 2026 最新消息"}',
            },
        )
    )
    await renderer.emit(
        ConversationEvent(
            type="tool_end",
            data={
                "tool_name": "web_search",
                "tool_use_id": "t1",
                "status": "success",
                "output_summary": "Found 10 results",
                "duration": 1.23,
            },
        )
    )

    text = stream.getvalue()
    assert '● Web Search "巧乐兹 2026 最新消息"' in text
    assert "巧乐兹" in text
    assert "└ Found 10 results · 1.2s" in text
    assert "工具:" not in text
    assert "╭" not in text
    assert "╰" not in text


@pytest.mark.asyncio
async def test_cli_shell_truncates_tool_results_by_default():
    renderer, stream = _renderer()

    await renderer.emit(
        ConversationEvent(
            type="tool_end",
            data={
                "tool_name": "web_fetch",
                "tool_use_id": "t2",
                "status": "error",
                "error": "x" * 160,
                "duration": 0.6,
            },
        )
    )

    text = stream.getvalue()
    assert "● Web Fetch" in text
    assert "└ " in text
    assert "+60 chars" in text
    assert "x" * 160 not in text


@pytest.mark.asyncio
async def test_cli_shell_formats_tool_json_args_for_humans():
    renderer, stream = _renderer()

    await renderer.emit(
        ConversationEvent(
            type="tool_start",
            data={
                "tool_name": "weather",
                "tool_use_id": "t3",
                "input_summary": '{"city": "火星", "units": "metric", "days": 3}',
            },
        )
    )
    await renderer.emit(
        ConversationEvent(
            type="tool_end",
            data={
                "tool_name": "weather",
                "tool_use_id": "t3",
                "status": "success",
                "output_summary": "晴",
                "duration": 1.5,
            },
        )
    )

    text = stream.getvalue()
    assert "● Weather city=火星 · units=metric · days=3" in text
    assert '{"city"' not in text


@pytest.mark.asyncio
async def test_cli_shell_failed_tool_uses_error_color():
    renderer, stream = _renderer()

    await renderer.emit(
        ConversationEvent(
            type="tool_end",
            data={
                "tool_name": "fly_to_moon",
                "tool_use_id": "t4",
                "status": "error",
                "error": "unknown tool 'fly_to_moon'",
                "input_summary": '{"speed": "fast"}',
                "duration": 0.0,
            },
        )
    )

    text = stream.getvalue()
    assert "●" in text
    assert "Fly To Moon speed=fast" in text
    assert "unknown tool 'fly_to_moon'" in text
    assert renderer._tool_dot_style("error") == "red"
    assert renderer._tool_dot_style("success") == "green"


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


@pytest.mark.asyncio
async def test_cli_shell_multiline_input_submits_literal_text():
    output: list[str] = []
    renderer, stream = _renderer()
    runtime = FakeRuntime()
    inputs = iter(['"""', "line1", "", "/help", '"""', ""])

    async def input_fn(prompt: str) -> str:
        output.append(prompt)
        return next(inputs)

    shell = CliShell(runtime, input_fn=input_fn, renderer=renderer)

    await shell.run()

    assert runtime.messages == ["line1\n\n/help"]
    assert "/help" not in runtime.commands
    assert "│ " in output
    assert "echo:line1" in stream.getvalue()


@pytest.mark.asyncio
async def test_cli_shell_multiline_cancel_does_not_run_message():
    output: list[str] = []
    renderer, stream = _renderer()
    runtime = FakeRuntime()
    inputs = iter(['"""', "line1", "/cancel", ""])

    async def input_fn(prompt: str) -> str:
        output.append(prompt)
        return next(inputs)

    shell = CliShell(runtime, input_fn=input_fn, renderer=renderer)

    await shell.run()

    assert runtime.messages == []
    assert "已取消多行输入。" in stream.getvalue()
    assert "│ " in output


@pytest.mark.asyncio
async def test_cli_shell_ctrl_c_during_turn_requests_stop_and_continues():
    output: list[str] = []
    renderer, stream = _renderer()
    runtime = CancellableRuntime()
    inputs = iter(["first", "second", ""])

    async def input_fn(prompt: str) -> str:
        output.append(prompt)
        return next(inputs)

    shell = CliShell(runtime, input_fn=input_fn, renderer=renderer)

    run_task = asyncio.create_task(shell.run())
    await runtime.started.wait()
    run_task.cancel()
    await run_task

    assert runtime.stop_calls == 1
    assert runtime.messages == ["first", "second"]
    assert "停止:" in stream.getvalue()
    assert "echo:second" in stream.getvalue()


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
