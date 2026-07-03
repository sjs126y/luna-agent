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
    assert "Personal Agent" in text
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
async def test_cli_shell_run_echoes_user_input_and_reply():
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
    # input_fn path does not use prompt_toolkit, so the shell echoes the user line
    assert "› 你好" in text
    assert "Personal Agent" in text
    assert any(item == "› " for item in output)
    assert runtime.messages == ["你好"]


def test_cli_shell_prompt_status_bar_only():
    renderer, stream = _terminal_renderer(ShellRenderOptions(color=False))
    runtime = FakeRuntime()

    prompt = renderer.prompt(runtime)

    text = stream.getvalue()
    assert prompt == "› "
    # status bar prints, but no input frame / orange rule is drawn anymore
    assert "deepseek-chat" in text
    assert "\x1b[1A" not in text
    assert "\x1b[2C" not in text


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
    # Multiline is now Ctrl+J inside prompt_toolkit; the legacy `"""` sentinel
    # is just ordinary text sent as a single message.
    inputs = iter(['"""', ""])

    async def input_fn(prompt: str) -> str:
        output.append(prompt)
        return next(inputs)

    shell = CliShell(runtime, input_fn=input_fn, renderer=renderer)

    await shell.run()

    assert runtime.messages == ['"""']
    assert "echo:" in stream.getvalue()


def test_slash_completer_offers_matching_commands():
    from prompt_toolkit.document import Document

    from personal_agent.cli_shell import SLASH_COMMANDS, SlashCompleter

    completer = SlashCompleter(SLASH_COMMANDS)

    completions = list(completer.get_completions(Document("/se"), None))
    matches = [completion.text for completion in completions]
    assert "/session" in matches
    # Each completion carries a one-line hint in the menu.
    session = next(c for c in completions if c.text == "/session")
    assert "会话" in session.display_meta_text

    # No completions once the command is complete and args begin.
    assert list(completer.get_completions(Document("/session list"), None)) == []
    # No completions for plain text.
    assert list(completer.get_completions(Document("hello"), None)) == []


def test_slash_completer_includes_dynamic_skills(monkeypatch):
    from prompt_toolkit.document import Document

    from personal_agent.cli_shell import SLASH_COMMANDS, SlashCompleter

    class FakeSkill:
        def __init__(self, name, description):
            self.name = name
            self.description = description

    class FakeRegistry:
        def list(self):
            return [FakeSkill("web-dev", "前端开发技能")]

    monkeypatch.setattr(
        "personal_agent.skills.registry.skill_registry", FakeRegistry()
    )

    completer = SlashCompleter(SLASH_COMMANDS)
    completions = list(completer.get_completions(Document("/web"), None))
    texts = [c.text for c in completions]
    assert "/web-dev" in texts
    meta = next(c for c in completions if c.text == "/web-dev").display_meta_text
    assert "前端" in meta


@pytest.mark.asyncio
async def test_confirm_tool_renders_interaction_prompt():
    renderer, stream = _renderer()

    await renderer.emit(
        ConversationEvent(
            type="tool_end",
            data={
                "tool_name": "confirm",
                "tool_use_id": "c1",
                "status": "success",
                "output_summary": "删除 3 个文件？",
                "duration": 0.0,
            },
        )
    )

    text = stream.getvalue()
    assert "需要确认" in text
    assert "yes" in text
    assert "删除 3 个文件" in text
    # Not rendered as a low-key trace line.
    assert "● Confirm" not in text


@pytest.mark.asyncio
async def test_denied_tool_uses_warning_color():
    renderer, _ = _renderer()

    assert renderer._tool_dot_style("denied") == "yellow"
    assert renderer._tool_result_style("denied") == "yellow"
    assert renderer._tool_dot_style("interrupted") == "grey62"
    assert renderer._tool_dot_style("skipped") == "grey62"
    assert renderer._tool_dot_style("error") == "red"
    assert renderer._tool_dot_style("success") == "green"


def test_spinner_disabled_on_non_terminal_console():
    # StringIO-backed console is not a terminal, so the spinner stays off to
    # avoid polluting piped/captured output.
    renderer, _ = _renderer()
    assert renderer._spinner_enabled() is False


def test_spinner_disabled_in_verbose_and_quiet_modes():
    verbose, _ = _terminal_renderer(ShellRenderOptions(verbose=True))
    assert verbose._spinner_enabled() is False

    quiet, _ = _terminal_renderer(
        ShellRenderOptions(quiet_events=True, show_events=False)
    )
    assert quiet._spinner_enabled() is False


def test_spinner_enabled_on_plain_terminal():
    renderer, _ = _terminal_renderer()
    assert renderer._spinner_enabled() is True


def test_ctrl_j_inserts_newline_enter_submits():
    from prompt_toolkit.keys import Keys

    renderer, _ = _renderer()
    shell = CliShell(FakeRuntime(), renderer=renderer)

    bindings = shell._key_bindings()
    keymap = {tuple(binding.keys): binding for binding in bindings.bindings}

    # Ctrl+J inserts a newline into the buffer (Alt+Enter is grabbed by many terminals).
    ctrl_j = keymap[(Keys.ControlJ,)]
    inserted: list[str] = []
    newline_event = SimpleNamespace(
        current_buffer=SimpleNamespace(insert_text=inserted.append)
    )
    ctrl_j.handler(newline_event)
    assert inserted == ["\n"]

    # Plain Enter submits the current buffer.
    plain_enter = keymap[(Keys.ControlM,)]
    submitted: list[bool] = []
    submit_event = SimpleNamespace(
        current_buffer=SimpleNamespace(
            validate_and_handle=lambda: submitted.append(True)
        )
    )
    plain_enter.handler(submit_event)
    assert submitted == [True]


@pytest.mark.asyncio
async def test_cli_shell_echoes_user_input_on_non_terminal_path():
    renderer, stream = _renderer()
    runtime = FakeRuntime()
    shell = CliShell(runtime, renderer=renderer)

    await shell.run_once("你好呀")

    text = stream.getvalue()
    assert "› 你好呀" in text


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
