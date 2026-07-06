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
    async def on_tool_start(self, event: ConversationEvent) -> None:
        data = event.data
        name = str(data.get("tool_name") or "tool")
        tool_use_id = str(data.get("tool_use_id") or f"tool-{self.state.tool_seq + 1}")
        self.state.tool_seq += 1
        self.state.active_tools[tool_use_id] = ToolTrace(
            index=self.state.tool_seq,
            tool_use_id=tool_use_id,
            name=name,
            display_name=name,
            input_summary=str(data.get("input_summary") or ""),
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
                display_name=str(data.get("tool_name") or "tool"),
                input_summary=str(data.get("input_summary") or ""),
                started_at=time.monotonic(),
            )
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

    async def on_turn_end(self, event: ConversationEvent) -> None:
        self.state.streaming = False
        self.state.status_message = "ready"
        self._invalidate()

    # ── helpers ──────────────────────────────────────────
    def _tool_line(self, item: ToolTrace) -> str:
        ok = item.status in ("success", "ok", "")
        mark = theme.sgr("✓", theme.TOOL_OK) if ok else theme.sgr("✗", theme.TOOL_ERR)
        dur = f" {item.duration:.1f}s" if item.duration else ""
        summary = f" {item.input_summary}" if item.input_summary else ""
        return f"  ⚙ {item.display_name}{summary}  {mark}{dur}"
