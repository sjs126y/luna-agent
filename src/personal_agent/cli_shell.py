"""Terminal-first CLI shell renderer."""

from __future__ import annotations

import asyncio
import inspect
import sys
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from personal_agent.cli_chat import CliChatRuntime, create_cli_runtime
from personal_agent.conversation.events import ConversationEvent, ConversationEventSink


@dataclass
class ShellRenderOptions:
    color: bool = True
    show_events: bool = True
    verbose: bool = False
    quiet_events: bool = False
    max_tool_summary_chars: int = 140


class TerminalRenderer(ConversationEventSink):
    def __init__(
        self,
        *,
        output_fn: Callable[[str], None] | None = None,
        console: Console | None = None,
        options: ShellRenderOptions | None = None,
    ) -> None:
        self.options = options or ShellRenderOptions()
        self.output_fn = output_fn
        self.console = console or (
            None
            if output_fn is not None
            else Console(color_system="auto" if self.options.color else None)
        )
        self._turn_started_at: float | None = None
        self._llm_started_at: float | None = None
        self._last_llm_duration = 0.0
        self._last_input_tokens = 0
        self._last_output_tokens = 0

    async def emit(self, event: ConversationEvent) -> None:
        if event.type == "assistant_message":
            text = event.message.strip()
            if text:
                self.assistant_message(text)
            return

        if event.type == "turn_start":
            self._turn_started_at = time.monotonic()
            return

        if event.type == "llm_end":
            self._last_input_tokens = int(event.data.get("input_tokens", 0) or 0)
            self._last_output_tokens = int(event.data.get("output_tokens", 0) or 0)
            if self._llm_started_at is not None:
                self._last_llm_duration = max(0.0, time.monotonic() - self._llm_started_at)

        if self.options.quiet_events or not self.options.show_events:
            return

        if event.type == "llm_start":
            self._llm_started_at = time.monotonic()
            if self.options.verbose:
                self._event_line("模型", "请求中", style="dim cyan")
        elif event.type == "llm_end":
            if self.options.verbose:
                self._model_summary()
        elif event.type == "tool_start":
            text = f"{event.data.get('tool_name', '')} 开始"
            if self.options.verbose and event.data.get("input_summary"):
                text += f" {self._short(event.data['input_summary'])}"
            self._event_line("工具", text.strip(), style="yellow")
        elif event.type == "tool_end":
            status = event.data.get("status", "")
            summary = event.data.get("output_summary") or event.data.get("error") or ""
            if summary:
                summary = self._short(str(summary))
            self._event_line(
                "工具",
                f"{event.data.get('tool_name', '')} {status} {summary}".strip(),
                style="green" if status == "success" else "red",
            )
        elif event.type == "retry":
            self._event_line("重试", event.message, style="yellow")
        elif event.type == "compression":
            self._event_line("压缩", event.message, style="magenta")
        elif event.type == "stop":
            self._event_line("停止", event.message or "已停止", style="yellow")
        elif event.type == "error":
            self.error_text(event.data.get("error") or event.message)

    def banner(self, runtime: CliChatRuntime) -> None:
        provider = getattr(runtime.settings, "llm_provider", "")
        model = getattr(runtime.settings, "llm_model", "")
        title = Text("Personal Agent CLI", style="bold cyan")
        subtitle = Text(
            f"session={runtime.session_key} | provider={provider} | model={model}",
            style="dim",
        )
        self._print(
            Panel(
                Text.assemble(title, "\n", subtitle),
                border_style="blue",
                box=box.ROUNDED,
            )
        )
        self._print(Text("输入 exit/quit 或空行退出，/help 查看命令。", style="dim"))

    def prompt(self, runtime: CliChatRuntime) -> str:
        self.input_prompt(runtime)
        return "› "

    def input_prompt(self, runtime: CliChatRuntime) -> None:
        self._print(Text(self._status_text(runtime), style="bold blue"))
        self._rule("─", style="orange3")

    def user_message(self, text: str) -> None:
        self._turn_started_at = time.monotonic()
        self._last_llm_duration = 0.0
        self._last_input_tokens = 0
        self._last_output_tokens = 0
        self._rule("─", style="yellow")
        body = Text()
        body.append("● ", style="bold yellow")
        body.append(text, style="bright_white")
        self._print(body)
        self._rule("─", style="yellow")

    def assistant_message(self, text: str) -> None:
        lines = text.splitlines() or [text]
        renderables = [self._block_top("$ PersonalAgent", style="cyan")]
        for line in lines:
            renderables.append(Text(f"  {line}"))
        renderables.append(self._block_bottom(style="cyan"))
        self._print(Group(*renderables))

    def command_response(self, text: str) -> None:
        if not text:
            return
        if "\n" not in text and len(text) <= 100:
            body = Text()
            body.append("命令 ", style="bold blue")
            body.append(text)
            self._print(body)
        else:
            renderables = [self._block_top("命令", style="blue")]
            renderables.extend(Text(f"  {line}") for line in text.splitlines())
            renderables.append(self._block_bottom(style="blue"))
            self._print(Group(*renderables))

    def error(self, exc: Exception) -> None:
        self.error_text(f"本轮对话失败: {exc}")

    def error_text(self, text: str) -> None:
        self._print(
            Panel(
                text,
                title="错误",
                title_align="left",
                border_style="red",
                box=box.ROUNDED,
            )
        )

    def status_line(self, runtime: CliChatRuntime, result: object | None = None) -> None:
        self._print(Text(self._status_text(runtime, result=result), style="bold blue"))

    def _status_text(self, runtime: CliChatRuntime, result: object | None = None) -> str:
        settings = runtime.settings
        provider = getattr(settings, "llm_provider", "")
        model = getattr(settings, "llm_model", "")
        raw = getattr(result, "raw", {}) if result is not None else {}
        api_calls = raw.get("api_calls", "") if isinstance(raw, dict) else ""
        context_window = self._context_window(runtime)
        context = self._context_text(context_window)
        duration = 0.0
        if self._turn_started_at is not None:
            duration = max(0.0, time.monotonic() - self._turn_started_at)

        parts = [model or provider or "-"]
        if context:
            parts.append(context)
        if api_calls != "":
            parts.append(f"api={api_calls}")
        if self._last_input_tokens or self._last_output_tokens:
            parts.append(f"in={self._last_input_tokens} out={self._last_output_tokens}")
        parts.append(f"{duration:.1f}s")
        return " | ".join(parts)

    def _event_line(self, label: str, text: str, *, style: str = "dim") -> None:
        body = Text()
        body.append(f"{label}: ", style=f"bold {style}")
        body.append(text, style=style)
        self._print(body)

    def _model_summary(self) -> None:
        parts = []
        if self._last_input_tokens:
            parts.append(f"in {self._last_input_tokens}")
        if self._last_output_tokens:
            parts.append(f"out {self._last_output_tokens}")
        if self._last_llm_duration:
            parts.append(f"{self._last_llm_duration:.1f}s")
        self._event_line("模型", " · ".join(parts) or "完成", style="dim green")

    def _rule(self, char: str, *, style: str) -> None:
        width = self._console_width()
        self._print(Text(char * min(width, 72), style=style))

    def _block_top(self, title: str, *, style: str) -> Text:
        width = self._console_width()
        prefix = f"╭─ {title} "
        line = "─" * max(4, min(width, 72) - len(prefix))
        return Text(prefix + line, style=style)

    def _block_bottom(self, *, style: str) -> Text:
        width = self._console_width()
        return Text("╰" + "─" * max(4, min(width, 72) - 1), style=style)

    def _print(self, value) -> None:
        if self.console is not None:
            self.console.print(value)
            return
        if isinstance(value, Text):
            text = value.plain
        else:
            text = str(value)
        if self.output_fn is not None:
            self.output_fn(text)

    def _short(self, text: str) -> str:
        if self.options.verbose:
            return text
        value = " ".join(text.split())
        limit = max(20, self.options.max_tool_summary_chars)
        if len(value) <= limit:
            return value
        return value[: limit - 3] + "..."

    def _context_window(self, runtime: CliChatRuntime) -> int:
        service = getattr(runtime, "conversation_service", None)
        agent = None
        if service is not None:
            try:
                agent = service.get_cached_agent(runtime.session_key)
            except Exception:
                agent = None
        provider = getattr(agent, "_provider", None)
        return int(getattr(provider, "context_window", 0) or 0)

    def _context_text(self, context_window: int) -> str:
        if context_window <= 0 or self._last_input_tokens <= 0:
            return ""
        percent = round(self._last_input_tokens / max(context_window, 1) * 100, 1)
        return f"ctx {self._last_input_tokens:,}/{context_window:,} ({percent}%)"

    def _console_width(self) -> int:
        if self.console is not None:
            return int(getattr(self.console, "width", 80) or 80)
        return 80


class CliShell:
    def __init__(
        self,
        runtime: CliChatRuntime,
        *,
        input_fn: Callable[[str], str | Awaitable[str]] | None = None,
        renderer: TerminalRenderer | None = None,
    ) -> None:
        self.runtime = runtime
        self.input_fn = input_fn or _read_input
        self.renderer = renderer or TerminalRenderer()

    async def run(self) -> None:
        self.renderer.banner(self.runtime)
        while True:
            try:
                text = await _resolve_input(self.input_fn(self.renderer.prompt(self.runtime)))
            except (EOFError, KeyboardInterrupt):
                print()
                break
            text = text.strip()
            if not text or text.lower() in {"exit", "quit"}:
                break
            try:
                await self.run_once(text)
            except Exception as exc:
                self.renderer.error(exc)

    def run_sync(self, loop: asyncio.AbstractEventLoop) -> None:
        self.renderer.banner(self.runtime)
        while True:
            try:
                text = _resolve_input_sync(self.input_fn(self.renderer.prompt(self.runtime)), loop)
            except (EOFError, KeyboardInterrupt):
                print()
                break
            text = text.strip()
            if not text or text.lower() in {"exit", "quit"}:
                break
            try:
                loop.run_until_complete(self.run_once(text))
            except Exception as exc:
                self.renderer.error(exc)

    async def run_once(self, text: str) -> str:
        command_result = await self.runtime.handle_command(text)
        if command_result is not None:
            self.renderer.command_response(command_result)
            return command_result
        self.renderer.user_message(text)
        result = await self.runtime.run_message_events(text, event_sink=self.renderer)
        return result.final_response


async def run_cli_shell(
    *,
    session_name: str = "default",
    options: ShellRenderOptions | None = None,
) -> None:
    runtime = await create_cli_runtime(session_name=session_name)
    try:
        await CliShell(runtime, renderer=TerminalRenderer(options=options)).run()
    finally:
        await runtime.close()


def run_cli_shell_sync(
    *,
    session_name: str = "default",
    options: ShellRenderOptions | None = None,
) -> None:
    _configure_stdout()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    runtime = None
    try:
        runtime = loop.run_until_complete(create_cli_runtime(session_name=session_name))
        renderer = TerminalRenderer(options=options)
        CliShell(runtime, input_fn=_read_input_sync, renderer=renderer).run_sync(loop)
    finally:
        if runtime is not None:
            loop.run_until_complete(runtime.close())
        asyncio.set_event_loop(None)
        loop.close()


async def _read_input(prompt: str) -> str:
    return input(prompt)


def _read_input_sync(prompt_text: str) -> str:
    return input(prompt_text)


async def _resolve_input(value: str | Awaitable[str]) -> str:
    if inspect.isawaitable(value):
        return await value
    return value


def _resolve_input_sync(value: str | Awaitable[str], loop: asyncio.AbstractEventLoop) -> str:
    if inspect.isawaitable(value):
        return loop.run_until_complete(value)
    return value


def _configure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
