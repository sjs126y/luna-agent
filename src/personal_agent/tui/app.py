"""InlineTuiApp: the CC/Codex-style inline terminal app.

Owns the prompt_toolkit Application, wires an InlineRenderer to a UIState, and
runs each turn as a background task so the input box stays responsive. Finalized
content is pushed to scrollback via run_in_terminal (print above the prompt).

Phase 1 scope: one-turn-at-a-time read-only render + basic keys (Enter / Ctrl+C /
Ctrl+D). History, slash-completion and Ctrl+O expand come in Phase 2.
"""

from __future__ import annotations

import asyncio

from pathlib import Path
import sys
from typing import NamedTuple

from prompt_toolkit.application import Application
from prompt_toolkit.application.run_in_terminal import run_in_terminal
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout

from personal_agent.tui.layout import build_layout
from personal_agent.tui.renderer import InlineRenderer
from personal_agent.tui.state import UIState


class _PrintRequest(NamedTuple):
    text: str | None
    done: asyncio.Future[None] | None


class InlineTuiApp:
    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.state = UIState()
        self.state.exec_mode = "normal"
        self.state.model = getattr(getattr(runtime, "settings", None), "llm_model", "") or ""
        self.root, self.input_area = build_layout(
            self.state,
            completer=self._build_completer(),
            history=self._build_history(),
        )
        self.renderer = InlineRenderer(
            state=self.state,
            invalidate=self._invalidate,
            print_above=self._print_above,
            width=self._term_width(),
        )
        self._turn_task: asyncio.Task | None = None
        self._last_expanded: tuple[str, str] | None = None
        self._print_queue: asyncio.Queue[_PrintRequest] = asyncio.Queue()
        self._print_worker_task: asyncio.Task | None = None
        self.app: Application | None = None

    # ── reuse the classic shell's completer + history ──
    def _build_completer(self):
        try:
            from personal_agent.cli_shell import SLASH_COMMANDS, SlashCompleter

            return SlashCompleter(SLASH_COMMANDS)
        except Exception:
            return None

    def _build_history(self) -> History:
        try:
            data_dir = Path(getattr(self.runtime.settings, "agent_data_dir", "data"))
            data_dir.mkdir(parents=True, exist_ok=True)
            return FileHistory(str(data_dir / "cli_history.txt"))
        except Exception:
            return InMemoryHistory()

    # ── prompt_toolkit callbacks the renderer uses ──
    def _invalidate(self) -> None:
        if self.app is not None:
            self.app.invalidate()

    async def _print_above(self, text: str) -> None:
        if not text:
            return
        self._ensure_print_worker()
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        await self._print_queue.put(_PrintRequest(text, done))
        await done

    def _print_above_nowait(self, text: str) -> None:
        if not text:
            return
        self._ensure_print_worker()
        self._print_queue.put_nowait(_PrintRequest(text, None))

    def _ensure_print_worker(self) -> None:
        if self._print_worker_task is None or self._print_worker_task.done():
            self._print_worker_task = asyncio.create_task(self._print_worker())

    async def _print_worker(self) -> None:
        try:
            while True:
                request = await self._print_queue.get()
                if request.text is None:
                    if request.done is not None and not request.done.done():
                        request.done.set_result(None)
                    return

                requests = [request]
                while not self._print_queue.empty():
                    next_request = self._print_queue.get_nowait()
                    if next_request.text is None:
                        await self._flush_print_requests(requests)
                        if next_request.done is not None and not next_request.done.done():
                            next_request.done.set_result(None)
                        return
                    requests.append(next_request)

                await self._flush_print_requests(requests)
        finally:
            if self._print_worker_task is asyncio.current_task():
                self._print_worker_task = None

    async def _flush_print_requests(self, requests: list[_PrintRequest]) -> None:
        texts = [request.text for request in requests if request.text]
        if not texts:
            for request in requests:
                if request.done is not None and not request.done.done():
                    request.done.set_result(None)
            return

        payload = "\n".join(text.rstrip("\n") for text in texts)
        if not payload.endswith("\n"):
            payload += "\n"

        try:
            await run_in_terminal(lambda: self._write_terminal(payload))
        except Exception as exc:
            for request in requests:
                if request.done is not None and not request.done.done():
                    request.done.set_exception(exc)
            return

        for request in requests:
            if request.done is not None and not request.done.done():
                request.done.set_result(None)

    def _write_terminal(self, text: str) -> None:
        app = self.app
        if app is not None:
            try:
                app.output.enable_autowrap()
                app.output.write_raw(text)
                app.output.flush()
                return
            except Exception:
                pass
        sys.stdout.write(text)
        sys.stdout.flush()

    async def _stop_print_worker(self) -> None:
        task = self._print_worker_task
        if task is None:
            return
        if task.done():
            await task
            self._print_worker_task = None
            return
        done = asyncio.get_running_loop().create_future()
        await self._print_queue.put(_PrintRequest(None, done))
        await done
        await task
        self._print_worker_task = None

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
            await self._print_above(str(command_result))
            await self._refresh_mode()
            return
        await self._print_above(f"\x1b[1m你:\x1b[0m {text}")
        result = await self.runtime.run_message_events(text, event_sink=self.renderer)
        await self._refresh_mode()
        return result

    async def _refresh_mode(self) -> None:
        """Sync the status-bar execution mode from the runtime's current grants."""
        getter = getattr(self.runtime, "current_execution_mode", None)
        if getter is None:
            return
        try:
            mode = await getter()
        except Exception:
            return
        if mode and mode != self.state.exec_mode:
            self.state.exec_mode = mode
            self._invalidate()

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
            # If the completion menu is open, accept it instead of submitting,
            # so a bare '/' + Enter picks the top command (matches classic shell).
            buf = event.current_buffer
            state = buf.complete_state
            if state is not None:
                completion = state.current_completion or (
                    state.completions[0] if state.completions else None
                )
                if completion is not None:
                    buf.apply_completion(completion)
                    return
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

        @kb.add("c-o")
        def _(event) -> None:
            self._expand_last()

        @kb.add("c-d")
        def _(event) -> None:
            if not self.input_area.text:
                event.app.exit()

        return kb

    def _expand_last(self) -> None:
        """Print the most recent expandable output (tool result / thinking) into
        scrollback. Inline model: no full-screen pager, just print above.
        Dedup: pressing Ctrl+O repeatedly on the same content prints it once."""
        if self.state.last_expandable is None:
            return
        if self.state.last_expandable == self._last_expanded:
            return  # already expanded this content; don't restack
        self._last_expanded = self.state.last_expandable
        name, full = self.state.last_expandable
        header = f"\x1b[34m$ {name}\x1b[0m"
        body = "\n".join(f"  {line}" for line in (full.splitlines() or [full]))
        self._print_above_nowait(f"{header}\n{body}")

    async def run(self) -> None:
        self.app = Application(
            layout=Layout(self.root, focused_element=self.input_area),
            key_bindings=self._build_keys(),
            full_screen=False,
            mouse_support=False,
        )
        try:
            await self.app.run_async()
        finally:
            await self._stop_print_worker()


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
