"""Terminal-first CLI shell renderer."""

from __future__ import annotations

import asyncio
import inspect
import sys
from dataclasses import dataclass
from typing import Awaitable, Callable

from personal_agent.cli_chat import CliChatRuntime, create_cli_runtime
from personal_agent.conversation.events import ConversationEvent, ConversationEventSink


@dataclass
class ShellRenderOptions:
    color: bool = True
    show_events: bool = True


class TerminalRenderer(ConversationEventSink):
    def __init__(
        self,
        *,
        output_fn: Callable[[str], None] = print,
        options: ShellRenderOptions | None = None,
    ) -> None:
        self.output_fn = output_fn
        self.options = options or ShellRenderOptions()
        self._active_assistant = False

    async def emit(self, event: ConversationEvent) -> None:
        if not self.options.show_events:
            return
        if event.type == "llm_start":
            self._line("模型", "请求中")
        elif event.type == "llm_end":
            self._line(
                "模型",
                f"完成 in={event.data.get('input_tokens', 0)} out={event.data.get('output_tokens', 0)}",
            )
        elif event.type == "assistant_message":
            text = event.message.strip()
            if text:
                self._assistant(text)
        elif event.type == "tool_start":
            self._line("工具", f"{event.data.get('tool_name', '')} 开始")
        elif event.type == "tool_end":
            status = event.data.get("status", "")
            summary = event.data.get("output_summary") or event.data.get("error") or ""
            self._line("工具", f"{event.data.get('tool_name', '')} {status} {summary}".strip())
        elif event.type == "retry":
            self._line("重试", event.message)
        elif event.type == "compression":
            self._line("压缩", event.message)
        elif event.type == "stop":
            self._line("停止", event.message or "已停止")
        elif event.type == "error":
            self._line("错误", event.data.get("error") or event.message)

    def banner(self, runtime: CliChatRuntime) -> None:
        provider = getattr(runtime.settings, "llm_provider", "")
        model = getattr(runtime.settings, "llm_model", "")
        self.output_fn(
            f"Personal Agent CLI | session={runtime.session_key} | provider={provider} | model={model}"
        )
        self.output_fn("输入 exit/quit 或空行退出，/help 查看命令。")

    def prompt(self, runtime: CliChatRuntime) -> str:
        return f"\n{runtime.session_key} >>> "

    def command_response(self, text: str) -> None:
        if text:
            self.output_fn(text)

    def error(self, exc: Exception) -> None:
        self._line("错误", f"本轮对话失败: {exc}")

    def _assistant(self, text: str) -> None:
        self.output_fn(f"\nassistant:\n{text}")

    def _line(self, label: str, text: str) -> None:
        if text:
            self.output_fn(f"[{label}] {text}")
        else:
            self.output_fn(f"[{label}]")


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
        result = await self.runtime.run_message_events(text, event_sink=self.renderer)
        return result.final_response


async def run_cli_shell(*, session_name: str = "default") -> None:
    runtime = await create_cli_runtime(session_name=session_name)
    try:
        await CliShell(runtime).run()
    finally:
        await runtime.close()


def run_cli_shell_sync(*, session_name: str = "default") -> None:
    _configure_stdout()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    runtime = None
    try:
        runtime = loop.run_until_complete(create_cli_runtime(session_name=session_name))
        CliShell(runtime, input_fn=_read_input_sync).run_sync(loop)
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
