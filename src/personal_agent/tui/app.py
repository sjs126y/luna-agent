"""InlineTuiApp: the CC/Codex-style inline terminal app.

Owns the prompt_toolkit Application, wires an InlineRenderer to a UIState, and
runs each turn as a background task so the input box stays responsive. Finalized
content is pushed to scrollback via run_in_terminal (print above the prompt).

Phase 1 scope: one-turn-at-a-time read-only render + basic keys (Enter / Ctrl+C /
Ctrl+D). History, slash-completion and Ctrl+O expand come in Phase 2.
"""

from __future__ import annotations

import asyncio

from prompt_toolkit.application import Application
from prompt_toolkit.application.run_in_terminal import run_in_terminal
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout

from personal_agent.tui.layout import build_layout
from personal_agent.tui.renderer import InlineRenderer
from personal_agent.tui.state import UIState


class InlineTuiApp:
    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.state = UIState()
        self.state.exec_mode = "normal"
        self.state.model = getattr(getattr(runtime, "settings", None), "llm_model", "") or ""
        self.root, self.input_area = build_layout(self.state)
        self.renderer = InlineRenderer(
            state=self.state,
            invalidate=self._invalidate,
            print_above=self._print_above,
            width=self._term_width(),
        )
        self._turn_task: asyncio.Task | None = None
        self.app: Application | None = None

    # ── prompt_toolkit callbacks the renderer uses ──
    def _invalidate(self) -> None:
        if self.app is not None:
            self.app.invalidate()

    def _print_above(self, text: str) -> None:
        # Schedule a print above the app; enters native scrollback.
        asyncio.ensure_future(run_in_terminal(lambda: print(text)))

    def _term_width(self) -> int:
        try:
            import shutil
            return max(20, shutil.get_terminal_size().columns - 2)
        except Exception:
            return 80

    # ── turn handling ──
    async def _submit(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        # Slash / builtin commands go through the runtime, result printed above.
        command_result = await self.runtime.handle_command(text)
        if command_result is not None:
            self._print_above(str(command_result))
            return
        self._print_above(f"\x1b[1m你:\x1b[0m {text}")
        result = await self.runtime.run_message_events(text, event_sink=self.renderer)
        return result

    def _on_enter(self) -> None:
        text = self.input_area.text
        self.input_area.text = ""
        if text.strip() in ("exit", "quit", "/exit", "/quit"):
            if self.app is not None:
                self.app.exit()
            return
        if self._turn_task and not self._turn_task.done():
            return  # a turn is already running
        self._turn_task = asyncio.ensure_future(self._submit(text))

    async def _stop(self) -> None:
        stop_agents = getattr(self.runtime, "stop_agents", None)
        if stop_agents is not None:
            value = stop_agents()
            if asyncio.iscoroutine(value):
                await value
        else:
            try:
                await self.runtime.handle_command("/stop")
            except Exception:
                pass

    def _build_keys(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter")
        def _(event) -> None:
            self._on_enter()

        @kb.add("c-j")
        def _(event) -> None:
            self.input_area.buffer.insert_text("\n")

        @kb.add("c-c")
        def _(event) -> None:
            if self._turn_task and not self._turn_task.done():
                asyncio.ensure_future(self._stop())
            else:
                self.input_area.text = ""

        @kb.add("c-d")
        def _(event) -> None:
            if not self.input_area.text:
                event.app.exit()

        return kb

    async def run(self) -> None:
        self.app = Application(
            layout=Layout(self.root, focused_element=self.input_area),
            key_bindings=self._build_keys(),
            full_screen=False,
            mouse_support=False,
        )
        await self.app.run_async()


async def run_inline_tui(*, session_name: str = "default") -> None:
    """Build a CLI runtime and drive it with the inline TUI."""
    from personal_agent.cli_chat import create_cli_runtime

    runtime = await create_cli_runtime(session_name=session_name)
    try:
        await InlineTuiApp(runtime).run()
    finally:
        await runtime.close()


def run_inline_tui_sync(*, session_name: str = "default") -> None:
    asyncio.run(run_inline_tui(session_name=session_name))
