"""Terminal-first CLI shell renderer."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.widgets import Frame, TextArea
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from personal_agent.cli_chat import CliChatRuntime, create_cli_runtime
from personal_agent.conversation.events import ConversationEvent, ConversationEventSink
from personal_agent.tui.renderer_base import Renderer

# Sentinel returned by PromptSession when Ctrl+O is pressed with expandable content.
# _read_line() detects this and launches the overlay Application instead of
# treating it as user input.
_EXPAND_SENTINEL = "\x00__expand__"

# Core slash commands offered by the input completer, with one-line hints
# shown in the completion menu. Skills are added dynamically at runtime.
SLASH_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/help", "显示帮助"),
    ("/new", "重置当前会话"),
    ("/session", "管理会话 (list/switch/rename/delete)"),
    ("/usage", "查看上下文预算"),
    ("/allow", "授权危险操作 (write/bash/all)"),
    ("/mode", "切换执行模式 (Read Only/Ask First/Edit Freely/Full Auto)"),
    ("/stop", "停止当前处理"),
    ("/export", "导出会话 JSONL"),
    ("/agents", "查看子 agent 运行记录"),
    ("/activity", "查看子 agent、后台任务和 Gateway agent 活动"),
    ("/memory", "查看和管理记忆"),
)


@dataclass
class ShellRenderOptions:
    color: bool = True
    show_events: bool = True
    verbose: bool = False
    quiet_events: bool = False
    max_tool_summary_chars: int = 140
    spinner: bool = True


@dataclass
class ToolTraceItem:
    index: int
    tool_use_id: str
    name: str
    display_name: str
    input_summary: str = ""
    started_at: float = 0.0
    status: str = "running"
    output_summary: str = ""
    full_output: str = ""
    error: str = ""
    duration: float = 0.0
    expandable: bool = False


class TerminalRenderer(Renderer):
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
        self._last_api_calls = 0
        self._last_model = ""
        self._last_context_window = 0
        self._last_context_used_tokens = 0
        self._last_context_percent = 0.0
        self._tool_seq = 0
        self._active_tools: dict[str, ToolTraceItem] = {}
        self._completed_tools: list[ToolTraceItem] = []
        self._status = None  # rich Status handle while a spinner is live
        self._live = None  # rich Live handle while streaming a reply
        self._stream_text = ""       # accumulated answer text this LLM call
        self._stream_thinking = ""   # accumulated thinking text this LLM call
        self._last_expandable = None  # (display_name, full_output) for Ctrl+O

    @property
    def wants_deltas(self) -> bool:
        # Opt into token-by-token events only when we can actually live-render
        # them; otherwise the loop skips per-token overhead entirely.
        return self._can_stream()

    def _can_stream(self) -> bool:
        return (
            not self.options.quiet_events
            and self.options.show_events
            and self.console is not None
            and bool(getattr(self.console, "is_terminal", False))
        )

    def _stream_view(self):
        parts = []
        if self._stream_thinking:
            parts.append(
                Text(f"💭 思考中… ({len(self._stream_thinking)} 字)", style="dim")
            )
        if self._stream_text:
            parts.append(
                Panel(
                    Text(self._stream_text),
                    title="Personal Agent",
                    title_align="left",
                    border_style="cyan",
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )
        return Group(*parts)

    def _ensure_live(self) -> bool:
        if not self._can_stream():
            return False
        if self._live is None:
            self._stop_spinner()
            try:
                self._live = Live(
                    self._stream_view(),
                    console=self.console,
                    auto_refresh=False,
                    transient=True,
                )
                self._live.start()
            except Exception:
                self._live = None
                return False
        return True

    def _update_live(self) -> None:
        if self._live is not None:
            try:
                self._live.update(self._stream_view())
                self._live.refresh()
            except Exception:
                pass

    def _stop_live(self) -> None:
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    def _finalize_stream(self) -> None:
        """Stop the live preview and leave a one-line thinking summary."""
        self._stop_live()
        if self._stream_thinking:
            thinking_text = self._stream_thinking
            # Store thinking as expandable so Ctrl+O can reveal it.
            self._last_expandable = ("💭 思考过程", thinking_text)
            self._print_impl(
                Text(f"  💭 已思考 {len(thinking_text)} 字  (Ctrl+O 展开)", style="dim")
            )
        self._stream_text = ""
        self._stream_thinking = ""

    def _spinner_enabled(self) -> bool:
        return (
            self.options.spinner
            and not self.options.verbose
            and not self.options.quiet_events
            and self.options.show_events
            and self.console is not None
            and bool(getattr(self.console, "is_terminal", False))
        )

    def _start_spinner(self, message: str) -> None:
        if not self._spinner_enabled():
            return
        self._stop_spinner()
        try:
            self._status = self.console.status(f"[dim cyan]{message}", spinner="dots")
            self._status.start()
        except Exception:
            self._status = None

    def _stop_spinner(self) -> None:
        if self._status is not None:
            try:
                self._status.stop()
            except Exception:
                pass
            self._status = None

    def _print(self, value) -> None:
        # A live spinner/preview and printed output cannot share the terminal;
        # drop both before writing so the trace/reply lands cleanly.
        self._stop_spinner()
        self._stop_live()
        self._print_impl(value)

    def _events_muted(self) -> bool:
        # Delta / assistant_message / turn_start / turn_end are NOT gated by this;
        # llm_start / tool_* / retry / compression / stop / error are.
        return self.options.quiet_events or not self.options.show_events

    async def on_assistant_delta(self, event: ConversationEvent) -> None:
        chunk = event.data.get("chunk") or ""
        if chunk:
            # Accumulate unconditionally; only the live preview is gated, so
            # state survives even if the terminal can't host a Live render.
            self._stream_text += chunk
            if self._ensure_live():
                self._update_live()

    async def on_thinking_delta(self, event: ConversationEvent) -> None:
        chunk = event.data.get("chunk") or ""
        if chunk:
            self._stream_thinking += chunk
            if self._ensure_live():
                self._update_live()

    async def on_assistant_message(self, event: ConversationEvent) -> None:
        text = event.message.strip()
        # Stop the live text preview, then render the final reply once as
        # markdown. Streaming showed plain text; this is the polished pass.
        self._finalize_stream()
        if text:
            self.assistant_message(text)

    async def on_turn_start(self, event: ConversationEvent) -> None:
        self.begin_turn()

    async def on_turn_end(self, event: ConversationEvent) -> None:
        self._finalize_stream()
        self._stop_spinner()

    async def on_llm_start(self, event: ConversationEvent) -> None:
        if self._events_muted():
            return
        self._llm_started_at = time.monotonic()
        if self.options.verbose:
            self._event_line("模型", "请求中", style="dim cyan")
        else:
            self._start_spinner("思考中…")

    async def on_llm_end(self, event: ConversationEvent) -> None:
        # State update happens unconditionally (status bar needs it even when
        # events are muted); only the verbose render below is gated.
        self._last_input_tokens = int(event.data.get("input_tokens", 0) or 0)
        self._last_output_tokens = int(event.data.get("output_tokens", 0) or 0)
        self._last_api_calls = int(event.data.get("api_calls", 0) or self._last_api_calls)
        model = str(event.data.get("model") or "")
        if model:
            self._last_model = model
        context_window = int(event.data.get("context_window", 0) or 0)
        if context_window:
            self._last_context_window = context_window
        context_used = int(event.data.get("context_used_tokens", 0) or 0)
        if context_used:
            self._last_context_used_tokens = context_used
        context_percent = float(event.data.get("context_percent", 0.0) or 0.0)
        if context_percent:
            self._last_context_percent = context_percent
        if self._llm_started_at is not None:
            self._last_llm_duration = max(0.0, time.monotonic() - self._llm_started_at)

        if self._events_muted():
            return
        if self.options.verbose:
            self._model_summary()

    async def on_tool_start(self, event: ConversationEvent) -> None:
        if self._events_muted():
            return
        self.tool_start(event)
        name = self._tool_display_name(str(event.data.get("tool_name") or "tool"))
        self._start_spinner(f"调用工具 {name}…")

    async def on_tool_end(self, event: ConversationEvent) -> None:
        if self._events_muted():
            return
        self.tool_end(event)

    async def on_retry(self, event: ConversationEvent) -> None:
        if self._events_muted():
            return
        self._event_line("重试", event.message, style="yellow")

    async def on_compression(self, event: ConversationEvent) -> None:
        if self._events_muted():
            return
        self._event_line("压缩", event.message, style="magenta")

    async def on_stop(self, event: ConversationEvent) -> None:
        if self._events_muted():
            return
        self._event_line("停止", event.message or "已停止", style="yellow")

    async def on_error(self, event: ConversationEvent) -> None:
        if self._events_muted():
            return
        self.error_text(event.data.get("error") or event.message)

    def banner(self, runtime: CliChatRuntime) -> None:
        provider = getattr(runtime.settings, "llm_provider", "")
        model = getattr(runtime.settings, "llm_model", "")
        if model:
            self._last_model = model
        title = Text("Personal Agent CLI", style="bold cyan")
        subtitle = Text(
            f"session={runtime.session_key} | provider={provider} | model={model}",
            style="dim",
        )
        self._print(
            Panel(
                Text.assemble(title, "\n", subtitle),
                border_style="dim cyan",
                box=box.SQUARE,
                padding=(0, 1),
            )
        )
        self._print(
            Text(
                "exit/quit 或空行退出，/help 查看命令，Ctrl+J 换行。",
                style="dim",
            )
        )

    def prompt(self, runtime: CliChatRuntime) -> str:
        # A thin rule separates the previous turn from the input region, then
        # the status bar sits just above the prompt line.
        self._print(Text("─" * self._line_width(), style="dim"))
        self._print(self._status_bar(runtime))
        return "› "

    def begin_turn(self) -> None:
        self._turn_started_at = time.monotonic()
        self._last_llm_duration = 0.0
        self._last_input_tokens = 0
        self._last_output_tokens = 0
        self._last_api_calls = 0

    def user_message(self, text: str) -> None:
        lines = text.splitlines() or [text]
        body = Text()
        body.append("› ", style="bold bright_white")
        body.append(lines[0], style="bright_white")
        for line in lines[1:]:
            body.append("\n  ")
            body.append(line, style="bright_white")
        self._print(body)

    def assistant_message(self, text: str) -> None:
        self._print(
            Panel(
                Markdown(text),
                title="Personal Agent",
                title_align="left",
                border_style="cyan",
                box=box.ROUNDED,
                padding=(0, 1),
                expand=False,  # shrink the box to the widest line, not full width
            )
        )
        # Blank line after the reply to separate it from the next user turn.
        self._print_impl(Text(""))

    def command_response(self, text: str) -> None:
        if not text:
            return
        if "\n" not in text and len(text) <= 100:
            body = Text()
            body.append("$ ", style="bold cyan")
            body.append(text)
            self._print(body)
        else:
            renderables = [self._block_top("$ command", style="blue")]
            renderables.extend(Text(f"  {line}") for line in text.splitlines())
            renderables.append(self._block_bottom(style="blue"))
            self._print(Group(*renderables))

    def tool_start(self, event: ConversationEvent) -> None:
        tool_name = str(event.data.get("tool_name") or "tool")
        tool_use_id = str(event.data.get("tool_use_id") or f"tool-{self._tool_seq + 1}")
        self._tool_seq += 1
        item = ToolTraceItem(
            index=self._tool_seq,
            tool_use_id=tool_use_id,
            name=tool_name,
            display_name=self._tool_display_name(tool_name),
            input_summary=self._clean_inline(str(event.data.get("input_summary") or "")),
            started_at=time.monotonic(),
        )
        self._active_tools[tool_use_id] = item

    def tool_end(self, event: ConversationEvent) -> None:
        tool_name = str(event.data.get("tool_name") or "tool")
        tool_use_id = str(event.data.get("tool_use_id") or "")
        item = self._active_tools.pop(tool_use_id, None)
        if item is None:
            self._tool_seq += 1
            item = ToolTraceItem(
                index=self._tool_seq,
                tool_use_id=tool_use_id or f"tool-{self._tool_seq}",
                name=tool_name,
                display_name=self._tool_display_name(tool_name),
                input_summary=self._clean_inline(str(event.data.get("input_summary") or "")),
                started_at=time.monotonic(),
            )
        item.status = str(event.data.get("status") or "")
        item.error = str(event.data.get("error") or "")
        item.output_summary = str(event.data.get("output_summary") or "")
        item.full_output = str(event.data.get("full_output") or "")
        item.duration = float(event.data.get("duration") or 0.0)
        if item.duration <= 0 and item.started_at:
            item.duration = max(0.0, time.monotonic() - item.started_at)
        self._completed_tools.append(item)
        # Remember the most recent full output so Ctrl+O can expand it.
        full = item.error or item.full_output or item.output_summary
        if full and full != item.output_summary:
            item.expandable = True
        if full:
            self._last_expandable = (item.display_name, full)
        if item.name in {"confirm", "clarify"} and item.status == "success":
            self._interaction_prompt(item)
            return
        self._print(self._tool_start_text(item))
        self._print(self._tool_result_text(item))

    def _interaction_prompt(self, item: ToolTraceItem) -> None:
        """Render confirm/clarify tool output as a prominent 'needs your reply' box."""
        text = item.output_summary or item.status
        if item.name == "confirm":
            title = "● 需要确认 — 请回复 yes / no"
        else:
            title = "● 需要澄清 — 请回复你的答案"
        self._print(
            Panel(
                Text(text),
                title=title,
                title_align="left",
                border_style="yellow",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )

    def expand_last_output(self) -> None:
        """Print the most recent expandable content (tool output or thinking). Bound to Ctrl+O."""
        if self._last_expandable is None:
            # Nothing to expand — silently do nothing rather than printing a box.
            return
        name, full = self._last_expandable
        renderables = [self._block_top(f"$ {name}", style="blue")]
        renderables.extend(Text(f"  {line}") for line in full.splitlines() or [full])
        renderables.append(self._block_bottom(style="blue"))
        self._print(Group(*renderables))

    def stop_text(self, text: str) -> None:
        self._event_line("停止", text or "已请求停止当前处理", style="yellow")

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
        self._print(self._status_bar(runtime, result=result))

    def update_result(self, result: object) -> None:
        raw = getattr(result, "raw", {})
        if isinstance(raw, dict):
            api_calls = raw.get("api_calls")
            if api_calls is not None:
                self._last_api_calls = int(api_calls or 0)

    def _status_text(self, runtime: CliChatRuntime, result: object | None = None) -> str:
        settings = runtime.settings
        provider = getattr(settings, "llm_provider", "")
        model = self._last_model or getattr(settings, "llm_model", "")
        raw = getattr(result, "raw", {}) if result is not None else {}
        api_calls = raw.get("api_calls", "") if isinstance(raw, dict) else ""
        if api_calls != "":
            self._last_api_calls = int(api_calls or 0)
        api_calls = self._last_api_calls
        context = self._context_text()
        duration = 0.0
        if self._turn_started_at is not None:
            duration = max(0.0, time.monotonic() - self._turn_started_at)

        parts = [model or provider or "-"]
        if context:
            parts.append(context)
        if api_calls != "":
            parts.append(f"api {api_calls}")
        if self._last_input_tokens or self._last_output_tokens:
            parts.append(
                f"in {self._format_count(self._last_input_tokens)} "
                f"out {self._format_count(self._last_output_tokens)}"
            )
        parts.append(f"{duration:.1f}s")
        return " | ".join(parts)

    def _event_line(self, label: str, text: str, *, style: str = "dim") -> None:
        body = Text()
        body.append(f"{label}: ", style=f"bold {style}")
        body.append(text, style=style)
        self._print(body)

    def _tool_start_text(self, item: ToolTraceItem) -> Text:
        dot_style = self._tool_dot_style(item.status)
        body = Text()
        body.append("● ", style=dot_style)
        body.append(item.display_name, style="bright_white")
        args = self._format_tool_args(item.input_summary)
        if args:
            body.append(args, style="dim")
        return body

    def _tool_result_text(self, item: ToolTraceItem) -> Text:
        result, truncated = self._truncate_result(item.error or item.output_summary or item.status)
        if not result:
            result = item.status or "done"
        body = Text()
        body.append("  └ ", style="dim")
        body.append(result, style=self._tool_result_style(item.status))
        body.append(f" · {item.duration:.1f}s", style="dim")
        if truncated and item.expandable:
            body.append("  Ctrl+O 展开", style="dim cyan")
        return body

    def _tool_dot_style(self, status: str) -> str:
        if status in {"", "running", "success"}:
            return "green"
        if status == "denied":
            return "yellow"
        if status in {"interrupted", "skipped"}:
            return "grey62"
        return "red"

    def _tool_result_style(self, status: str) -> str:
        if status in {"", "running", "success"}:
            return "dim"
        if status == "denied":
            return "yellow"
        if status in {"interrupted", "skipped"}:
            return "grey62"
        return "red"

    def _tool_display_name(self, name: str) -> str:
        overrides = {
            "web_search": "Web Search",
            "web_fetch": "Web Fetch",
            "file_write": "Write",
            "file_read": "Read",
            "file_edit": "Edit",
            "execute_code": "Execute Code",
            "bash": "Bash",
        }
        if name in overrides:
            return overrides[name]
        return " ".join(part.capitalize() for part in name.replace("-", "_").split("_") if part)

    def _format_tool_args(self, summary: str) -> str:
        formatted = self._human_tool_args(summary)
        if not formatted:
            return ""
        return f" {self._truncate_inline(formatted, limit=80)}"

    def _human_tool_args(self, summary: str) -> str:
        summary = self._clean_inline(summary)
        if not summary or summary == "{}":
            return ""
        parsed = self._parse_tool_summary(summary)
        if isinstance(parsed, dict):
            return self._format_tool_mapping(parsed)
        if isinstance(parsed, list):
            return f"items={len(parsed)}"
        if parsed is not None:
            return self._format_tool_value(parsed, quote_strings=True)
        return summary

    def _parse_tool_summary(self, summary: str) -> object | None:
        try:
            return json.loads(summary)
        except (TypeError, json.JSONDecodeError):
            return None

    def _format_tool_mapping(self, values: dict) -> str:
        if not values:
            return ""
        command = self._first_value(values, ("cmd", "command", "script"))
        if command:
            return f"$ {self._format_tool_value(command)}"
        prompt = self._first_value(values, ("query", "q", "search", "prompt", "text"))
        if prompt and len(values) == 1:
            return self._format_tool_value(prompt, quote_strings=True)
        path = self._first_value(values, ("path", "file", "url"))
        if path and len(values) == 1:
            return self._format_tool_value(path)

        parts = []
        for key, value in values.items():
            if value in (None, "", [], {}):
                continue
            parts.append(f"{key}={self._format_tool_value(value)}")
            if len(parts) >= 3:
                break
        hidden = len([value for value in values.values() if value not in (None, "", [], {})]) - len(parts)
        if hidden > 0:
            parts.append(f"+{hidden}")
        return " · ".join(parts)

    def _first_value(self, values: dict, keys: tuple[str, ...]) -> object | None:
        for key in keys:
            if key in values and values[key] not in (None, "", [], {}):
                return values[key]
        return None

    def _format_tool_value(self, value: object, *, quote_strings: bool = False) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            text = self._clean_inline(value)
            if quote_strings and text:
                return f'"{text}"'
            return text
        if isinstance(value, list):
            return f"[{len(value)}]"
        if isinstance(value, dict):
            return "{...}"
        return self._clean_inline(str(value))

    def _status_bar(self, runtime: CliChatRuntime, result: object | None = None) -> Text:
        status = self._status_text(runtime, result=result)
        parts = [part.strip() for part in status.split("|")]
        bar = Text()
        for index, part in enumerate(parts):
            if index:
                bar.append(" │ ", style="yellow on grey11")
            if index == 0:
                bar.append("$ ", style="bold cyan on grey11")
                bar.append(part, style="bold yellow on grey11")
            elif part.startswith("ctx "):
                bar.append(part, style="green on grey11")
            elif part.endswith("s"):
                bar.append(part, style="yellow on grey11")
            else:
                bar.append(part, style="bright_white on grey11")
        return bar

    def _model_summary(self) -> None:
        parts = []
        if self._last_input_tokens:
            parts.append(f"in {self._last_input_tokens}")
        if self._last_output_tokens:
            parts.append(f"out {self._last_output_tokens}")
        if self._last_llm_duration:
            parts.append(f"{self._last_llm_duration:.1f}s")
        self._event_line("模型", " · ".join(parts) or "完成", style="dim green")

    def _block_top(self, title: str, *, style: str) -> Text:
        prefix = f"╭─ {title} "
        max_width = self._line_width()
        line = "─" * max(4, max_width - len(prefix) - 1)
        return Text(prefix + line + "╮", style=style)

    def _block_bottom(self, *, style: str) -> Text:
        max_width = self._line_width()
        return Text("╰" + "─" * max(4, max_width - 2) + "╯", style=style)

    def _print_impl(self, value) -> None:
        if self.console is not None:
            self.console.print(value)
            return
        if isinstance(value, Text):
            text = value.plain
        else:
            text = str(value)
        if self.output_fn is not None:
            self.output_fn(text)

    def _clean_inline(self, text: str) -> str:
        return " ".join(str(text or "").split())

    def _truncate_inline(self, text: str, *, limit: int) -> str:
        value = self._clean_inline(text)
        if len(value) <= limit:
            return value
        return value[: max(1, limit - 3)] + "..."

    def _truncate_result(
        self,
        text: str,
        *,
        char_limit: int = 100,
        line_limit: int = 2,
    ) -> tuple[str, bool]:
        raw = str(text or "")
        lines = raw.splitlines()
        truncated = False
        if len(lines) > line_limit:
            raw = " ".join(lines[:line_limit])
            raw += f" ... +{len(lines) - line_limit} lines"
            truncated = True
        value = self._clean_inline(raw)
        if len(value) > char_limit:
            hidden = len(value) - char_limit
            value = value[: max(1, char_limit - 3)] + f"... +{hidden} chars"
            truncated = True
        return value, truncated

    def _context_text(self) -> str:
        used = self._last_context_used_tokens or self._last_input_tokens
        if self._last_context_window <= 0 or used <= 0:
            return ""
        percent = self._last_context_percent or round(used / max(self._last_context_window, 1) * 100, 1)
        return (
            f"ctx {self._format_count(used)}/"
            f"{self._format_count(self._last_context_window)} {percent}%"
        )

    def _console_width(self) -> int:
        if self.console is not None:
            return int(getattr(self.console, "width", 80) or 80)
        return 80

    def _console_height(self) -> int:
        if self.console is not None:
            height = getattr(self.console, "height", 0)
            if height:
                return int(height)
        import shutil

        return int(shutil.get_terminal_size((80, 24)).lines)

    def _line_width(self) -> int:
        return max(40, min(self._console_width(), 120))

    def _format_count(self, value: int) -> str:
        value = int(value or 0)
        abs_value = abs(value)
        if abs_value >= 1_000_000:
            compact = value / 1_000_000
            return f"{compact:g}M" if compact < 10 else f"{compact:.0f}M"
        if abs_value >= 10_000:
            compact = value / 1_000
            return f"{compact:.1f}K" if compact < 100 else f"{compact:.0f}K"
        if abs_value >= 1_000:
            compact = value / 1_000
            return f"{compact:g}K"
        return str(value)


class SlashCompleter(Completer):
    """Complete slash commands when the line starts with '/'.

    Core commands are static; skills are resolved lazily from the registry so
    newly loaded plugins show up without rebuilding the completer.
    """

    def __init__(self, commands: tuple[tuple[str, str], ...]) -> None:
        self.commands = commands

    def _skill_entries(self) -> list[tuple[str, str]]:
        try:
            from personal_agent.skills.registry import skill_registry

            return [
                (f"/{entry.name}", entry.description or "技能")
                for entry in skill_registry.list()
            ]
        except Exception:
            return []

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        seen: set[str] = set()
        for command, description in (*self.commands, *self._skill_entries()):
            if command in seen or not command.startswith(text):
                continue
            seen.add(command)
            yield Completion(
                command,
                start_position=-len(text),
                display_meta=description,
            )


class CliShell:
    def __init__(
        self,
        runtime: CliChatRuntime,
        *,
        input_fn: Callable[[str], str | Awaitable[str]] | None = None,
        renderer: TerminalRenderer | None = None,
    ) -> None:
        self.runtime = runtime
        self.input_fn = input_fn
        self.renderer = renderer or TerminalRenderer()
        self._session: PromptSession | None = None

    async def run(self) -> None:
        self.renderer.banner(self.runtime)
        if self._uses_prompt_toolkit():
            with patch_stdout(raw=True):
                await self._run_loop()
            return
        await self._run_loop()

    async def _run_loop(self) -> None:
        while True:
            try:
                text = await self._read_turn_text()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if text is None:
                break
            if text == "":
                continue
            try:
                await self.run_once(text)
            except Exception as exc:
                self.renderer.error(exc)

    def run_sync(self, loop: asyncio.AbstractEventLoop) -> None:
        loop.run_until_complete(self.run())

    async def run_once(self, text: str) -> str:
        command_result = await self.runtime.handle_command(text)
        if command_result is not None:
            self.renderer.command_response(command_result)
            return command_result
        if self._echo_input():
            self.renderer.user_message(text)
        self.renderer.begin_turn()
        result = await self._run_message_with_interrupt(text)
        self.renderer.update_result(result)
        return result.final_response

    async def _run_message_with_interrupt(self, text: str):
        task = asyncio.create_task(self.runtime.run_message_events(text, event_sink=self.renderer))
        try:
            return await asyncio.shield(task)
        except (KeyboardInterrupt, asyncio.CancelledError):
            message = await self._request_stop()
            self.renderer.stop_text(message)
            return await asyncio.shield(task)

    async def _request_stop(self) -> str:
        stop_agents = getattr(self.runtime, "stop_agents", None)
        if stop_agents is not None:
            value = stop_agents()
            if inspect.isawaitable(value):
                value = await value
            return str(value or "已请求停止当前处理")
        try:
            value = await self.runtime.handle_command("/stop")
        except Exception:
            return "已请求停止当前处理"
        return str(value or "已请求停止当前处理")

    async def _read_turn_text(self) -> str | None:
        value = await self._read_line(self.renderer.prompt(self.runtime))
        text = value.strip()
        if not text or text.lower() in {"exit", "quit"}:
            return None
        return text

    async def _read_line(self, prompt_text: str) -> str:
        if self.input_fn is not None:
            return await _resolve_input(self.input_fn(prompt_text))
        if self._uses_prompt_toolkit():
            # Ctrl+O exits the prompt with a sentinel; we open a scrollable
            # overlay, then re-prompt so the input line comes back unchanged.
            while True:
                result = await self._prompt_session().prompt_async(prompt_text)
                if result != _EXPAND_SENTINEL:
                    return result
                await self._show_expand_overlay()
        return await _read_input(prompt_text)

    async def _show_expand_overlay(self) -> None:
        expandable = getattr(self.renderer, "_last_expandable", None)
        if not expandable:
            return
        name, content = expandable
        try:
            await self._build_overlay_app(name, content).run_async()
        except Exception:
            # Fall back to inline printing if the overlay can't run.
            self.renderer.expand_last_output()

    def _uses_prompt_toolkit(self) -> bool:
        if self.input_fn is not None:
            return False
        console = self.renderer.console
        if console is None:
            return False
        return bool(getattr(console, "is_terminal", False))

    def _echo_input(self) -> bool:
        # prompt_toolkit leaves the submitted line in the scrollback; every
        # other input path (plain input(), input_fn, piped stdin) does not, so
        # the renderer echoes the user message to keep the transcript complete.
        return not self._uses_prompt_toolkit()

    def _prompt_session(self) -> PromptSession:
        if self._session is None:
            self._session = PromptSession(
                history=self._build_history(),
                completer=SlashCompleter(SLASH_COMMANDS),
                complete_while_typing=True,  # slash menu pops as you type '/'
                multiline=True,
                key_bindings=self._key_bindings(),
            )
        return self._session

    def _build_history(self):
        try:
            data_dir = Path(getattr(self.runtime.settings, "agent_data_dir", "data"))
            data_dir.mkdir(parents=True, exist_ok=True)
            return FileHistory(str(data_dir / "cli_history.txt"))
        except Exception:
            return InMemoryHistory()

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("c-j")  # Ctrl+J → newline (Alt+Enter is grabbed by many terminals)
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        @bindings.add("enter")  # Enter → accept the open completion, else submit
        def _(event) -> None:
            buf = event.current_buffer
            state = buf.complete_state
            if state is not None:
                # Menu is open: accept the highlighted item, or the first one if
                # nothing is highlighted yet (so a bare '/' + Enter picks top).
                completion = state.current_completion or (
                    state.completions[0] if state.completions else None
                )
                if completion is not None:
                    buf.apply_completion(completion)
                    return
            buf.validate_and_handle()

        @bindings.add("c-o")  # Ctrl+O → signal _read_line to open the expand overlay
        def _(event) -> None:
            if self.renderer._last_expandable is not None:
                event.app.exit(result=_EXPAND_SENTINEL)
            # Nothing to expand → silently ignore.

        return bindings

    def _build_overlay_app(self, name: str, content: str) -> Application:
        """Build a short-lived full-screen pager for one piece of output.

        It takes over the alternate screen (so the terminal is restored with no
        scrollback residue on exit), shows the full-width content, and lets the
        user scroll with the wheel / arrows / PageUp-Down / j-k / Ctrl+D-U.
        q or Esc closes it; the caller awaits ``app.run_async()`` and control
        returns to the PromptSession with the input line untouched.
        """
        kb = KeyBindings()

        @kb.add("escape")
        @kb.add("q")
        @kb.add("c-c")
        def _close(event) -> None:
            event.app.exit()

        text_area = TextArea(
            text=content,
            read_only=True,
            scrollbar=True,
            focusable=True,
            wrap_lines=False,
        )

        # Arrow keys / PageUp-Down move the read-only cursor natively; add
        # vim-style j/k and Ctrl+D/U half-page jumps on top.
        def _move(count: int) -> None:
            buf = text_area.buffer
            if count < 0:
                buf.cursor_up(count=-count)
            else:
                buf.cursor_down(count=count)

        def _half_page() -> int:
            size = self.renderer._console_height() - 2  # minus title + hint rows
            return max(1, size // 2)

        @kb.add("j")
        def _(event) -> None:
            _move(1)

        @kb.add("k")
        def _(event) -> None:
            _move(-1)

        @kb.add("c-d")
        def _(event) -> None:
            _move(_half_page())

        @kb.add("c-u")
        def _(event) -> None:
            _move(-_half_page())

        @kb.add("g")
        def _(event) -> None:
            text_area.buffer.cursor_position = 0

        @kb.add("G")
        def _(event) -> None:
            text_area.buffer.cursor_position = len(text_area.buffer.text)

        total_lines = max(1, content.count("\n") + 1)

        def _title() -> HTML:
            row = text_area.document.cursor_position_row + 1
            left = f" {name} · 完整输出"
            right = f"{row}/{total_lines} 行 "
            # Right-align the counter; CJK glyphs are double-width, so measure
            # display columns rather than character count.
            width = self.renderer._console_width()
            gap = max(2, width - _display_width(left) - _display_width(right))
            return HTML(
                f" <b>{name}</b> · 完整输出"
                f"{' ' * gap}<style fg='#888888'>{row}/{total_lines} 行</style> "
            )

        title_bar = Window(
            content=FormattedTextControl(_title),
            height=1,
            style="reverse",
        )
        hint_bar = Window(
            content=FormattedTextControl(
                HTML(" <b>滚轮 / ↑↓ / PgUp-Dn</b> 滚动  <b>g/G</b> 首尾  <b>q / Esc</b> 退出")
            ),
            height=1,
            style="reverse",
        )

        body = HSplit([title_bar, text_area, hint_bar])
        layout = Layout(container=body, focused_element=text_area)
        return Application(
            layout=layout,
            key_bindings=kb,
            mouse_support=True,   # wheel scrolls the content
            full_screen=True,     # alternate screen: clean restore, no residue
        )


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
    asyncio.run(run_cli_shell(session_name=session_name, options=options))


def _display_width(text: str) -> int:
    """Approximate terminal columns for a string, counting CJK glyphs as two."""
    import unicodedata

    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
    return width


async def _read_input(prompt: str) -> str:
    return input(prompt)


async def _resolve_input(value: str | Awaitable[str]) -> str:
    if inspect.isawaitable(value):
        return await value
    return value


def _configure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
