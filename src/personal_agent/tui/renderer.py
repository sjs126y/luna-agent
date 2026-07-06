"""InlineRenderer: map the ConversationEvent stream onto UIState.

This renderer NEVER touches the terminal directly. It mutates ``UIState`` and
calls two injected callbacks:
  - ``invalidate()``     — ask the app to repaint the bottom active region.
  - ``print_above(text)``— push finalized content into scrollback (the app wires
                           this to prompt_toolkit's run_in_terminal).
Both default to no-ops so the renderer can be unit-tested without a terminal.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import time

from personal_agent.conversation.events import ConversationEvent
from personal_agent.tui.markdown import render_markdown, render_plain
from personal_agent.tui.state import ToolTrace, UIState
from personal_agent.tui.renderer_base import Renderer
from personal_agent.tui import theme


def _noop() -> None: ...
async def _noop_print(_text: str) -> None: ...


class InlineRenderer(Renderer):
    wants_deltas = True

    def __init__(
        self,
        *,
        state: UIState | None = None,
        invalidate: Callable[[], None] = _noop,
        print_above: Callable[[str], Awaitable[None]] = _noop_print,
        width: int = 80,
    ) -> None:
        self.state = state or UIState()
        self._invalidate = invalidate
        self._print_above = print_above
        self.width = width
        self._llm_started_at: float | None = None

    # ── streaming ────────────────────────────────────────
    async def on_turn_start(self, event: ConversationEvent) -> None:
        self.state.reset_turn()
        self.state.status_message = "thinking…"
        self._invalidate()

    async def on_llm_start(self, event: ConversationEvent) -> None:
        self._llm_started_at = time.monotonic()
        self.state.status_message = "thinking…"
        self._invalidate()

    async def on_assistant_delta(self, event: ConversationEvent) -> None:
        chunk = event.data.get("chunk") or ""
        if not chunk:
            return
        self.state.stream_text += chunk
        self.state.streaming = True
        self._invalidate()

    async def on_thinking_delta(self, event: ConversationEvent) -> None:
        chunk = event.data.get("chunk") or ""
        if not chunk:
            return
        self.state.thinking_chars += len(chunk)
        self.state.streaming = True
        self._invalidate()

    async def on_assistant_message(self, event: ConversationEvent) -> None:
        text = event.message.strip()
        # Finalize: stop streaming preview, print the polished markdown reply
        # into scrollback, clear the active streaming state.
        self.state.stream_text = ""
        self.state.streaming = False
        if text:
            await self._print_above(render_markdown(text, width=self.width))
        self._invalidate()

    async def on_llm_end(self, event: ConversationEvent) -> None:
        data = event.data
        self.state.input_tokens = int(data.get("input_tokens", 0) or 0)
        self.state.output_tokens = int(data.get("output_tokens", 0) or 0)
        self.state.api_calls = int(data.get("api_calls", 0) or self.state.api_calls)
        model = str(data.get("model") or "")
        if model:
            self.state.model = model
        ctx = int(data.get("context_window", 0) or 0)
        if ctx:
            self.state.context_window = ctx
        self._invalidate()

    # ── tools ────────────────────────────────────────────
    async def on_tool_decision(self, event: ConversationEvent) -> None:
        data = event.data
        mode = str(data.get("execution_mode_label") or "")
        if mode:
            self.state.exec_mode = mode
        tool_use_id = str(data.get("tool_use_id") or "")
        item = self.state.active_tools.get(tool_use_id)
        if item is not None:
            display_name = str(data.get("display_name") or "")
            if display_name:
                item.display_name = display_name
            preview = str(data.get("input_preview") or data.get("input_summary") or "")
            if preview:
                item.input_preview = preview
            risk_level = str(data.get("risk_level") or "")
            if risk_level:
                item.risk_level = risk_level
            risk_summary = str(data.get("risk_summary") or "")
            if risk_summary:
                item.risk_summary = risk_summary
        self._invalidate()

    async def on_tool_start(self, event: ConversationEvent) -> None:
        data = event.data
        name = str(data.get("display_name") or data.get("tool_name") or "tool")
        tool_use_id = str(data.get("tool_use_id") or f"tool-{self.state.tool_seq + 1}")
        self.state.tool_seq += 1
        self.state.active_tools[tool_use_id] = ToolTrace(
            index=self.state.tool_seq,
            tool_use_id=tool_use_id,
            name=str(data.get("tool_name") or name),
            display_name=name,
            input_summary=str(data.get("input_summary") or ""),
            input_preview=str(data.get("input_preview") or data.get("input_summary") or ""),
            risk_level=str(data.get("risk_level") or ""),
            risk_summary=str(data.get("risk_summary") or ""),
            started_at=time.monotonic(),
        )
        self.state.status_message = f"calling {name}…"
        self._invalidate()

    async def on_tool_end(self, event: ConversationEvent) -> None:
        data = event.data
        tool_use_id = str(data.get("tool_use_id") or "")
        item = self.state.active_tools.pop(tool_use_id, None)
        if item is None:
            self.state.tool_seq += 1
            item = ToolTrace(
                index=self.state.tool_seq,
                tool_use_id=tool_use_id or f"tool-{self.state.tool_seq}",
                name=str(data.get("tool_name") or "tool"),
                display_name=str(data.get("display_name") or data.get("tool_name") or "tool"),
                input_summary=str(data.get("input_summary") or ""),
                input_preview=str(data.get("input_preview") or data.get("input_summary") or ""),
                risk_level=str(data.get("risk_level") or ""),
                risk_summary=str(data.get("risk_summary") or ""),
                started_at=time.monotonic(),
            )
        display_name = str(data.get("display_name") or "")
        if display_name:
            item.display_name = display_name
        preview = str(data.get("input_preview") or data.get("input_summary") or "")
        if preview:
            item.input_preview = preview
        risk_level = str(data.get("risk_level") or "")
        if risk_level:
            item.risk_level = risk_level
        risk_summary = str(data.get("risk_summary") or "")
        if risk_summary:
            item.risk_summary = risk_summary
        item.finish(
            status=str(data.get("status") or ""),
            output_summary=str(data.get("output_summary") or ""),
            full_output=str(data.get("full_output") or ""),
            error=str(data.get("error") or ""),
            duration=float(data.get("duration") or 0.0),
        )
        full = item.error or item.full_output or item.output_summary
        if full:
            self.state.last_expandable = (item.display_name, full)
        # Print the completed tool trace line into scrollback.
        await self._print_above(render_plain(self._tool_line(item), width=self.width))
        self._invalidate()

    async def on_retry(self, event: ConversationEvent) -> None:
        data = event.data
        text = event.message or "准备重试"
        attempt = data.get("attempt")
        max_attempts = data.get("max_attempts")
        suffix = ""
        if attempt and max_attempts:
            suffix = f" · {attempt}/{max_attempts}"
        elif attempt:
            suffix = f" · attempt {attempt}"
        names = data.get("tool_names") or data.get("tool_name") or ""
        if names:
            suffix += f" · {names}"
        await self._print_above(render_plain(theme.sgr(f"  ↻ {text}{suffix}", theme.NOTICE), width=self.width))

    async def on_compression(self, event: ConversationEvent) -> None:
        data = event.data
        text = event.message or "历史消息已压缩"
        pre = data.get("pre_message_count")
        post = data.get("post_message_count")
        suffix = f" · {pre} -> {post}" if pre is not None and post is not None else ""
        await self._print_above(render_plain(theme.sgr(f"  ◇ {text}{suffix}", theme.NOTICE), width=self.width))

    async def on_stop(self, event: ConversationEvent) -> None:
        text = event.message or str(event.data.get("message") or "已停止")
        self.state.streaming = False
        self.state.status_message = "stopped"
        await self._print_above(render_plain(theme.sgr(f"  ■ {text}", theme.NOTICE), width=self.width))
        self._invalidate()

    async def on_error(self, event: ConversationEvent) -> None:
        text = event.message or "运行错误"
        detail = str(event.data.get("error") or "")
        suffix = f": {detail}" if detail and detail not in text else ""
        self.state.streaming = False
        self.state.status_message = "error"
        await self._print_above(render_plain(theme.sgr(f"  ✗ {text}{suffix}", theme.ERROR), width=self.width))
        self._invalidate()

    async def on_turn_end(self, event: ConversationEvent) -> None:
        self.state.streaming = False
        self.state.status_message = "ready"
        self._invalidate()

    # ── helpers ──────────────────────────────────────────
    def _tool_line(self, item: ToolTrace) -> str:
        ok = item.status in ("success", "ok", "")
        mark = theme.sgr("✓", theme.TOOL_OK) if ok else theme.sgr("✗", theme.TOOL_ERR)
        dur = f" {item.duration:.1f}s" if item.duration else ""
        summary_text = item.input_preview or item.input_summary
        summary = f" {theme.dim(summary_text)}" if summary_text else ""
        if item.risk_summary and item.status in {"denied", "error"}:
            summary += f" {theme.dim(item.risk_summary)}"
        return f"  ⚙ {item.display_name}{summary}  {mark}{dur}"
