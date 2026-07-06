"""InlineTuiApp: the CC/Codex-style inline terminal app.

Owns the prompt_toolkit Application, wires an InlineRenderer to a UIState, and
runs each turn as a background task so the input box stays responsive. Finalized
content is pushed to scrollback via run_in_terminal (print above the prompt).

Current scope: selectable inline UI with prompt history/completion, slash
commands, Ctrl+O expansion, mode cycling, and inline tool-confirmation wiring.
"""

from __future__ import annotations

import asyncio

from pathlib import Path
import sys
from typing import NamedTuple

from prompt_toolkit.application import Application
from prompt_toolkit.application.run_in_terminal import run_in_terminal
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout

from personal_agent.tui.layout import build_layout
from personal_agent.tui.renderer import InlineRenderer
from personal_agent.tui.state import ConfirmPrompt, UIState
from personal_agent.tui import theme


class _PrintRequest(NamedTuple):
    text: str | None
    done: asyncio.Future[None] | None


def _decision_field(decision, name: str) -> str:
    """Read a field from a ToolDecision-like object (attr or dict)."""
    if decision is None:
        return ""
    if isinstance(decision, dict):
        return str(decision.get(name) or "")
    return str(getattr(decision, name, "") or "")


def _decision_list(decision, name: str) -> list[str]:
    value = decision.get(name) if isinstance(decision, dict) else getattr(decision, name, None)
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    if value:
        return [str(value)]
    return []


class InlineTuiApp:
    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.state = UIState()
        self.state.exec_mode = "Ask First"
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
        self._confirm_future: asyncio.Future[str] | None = None
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
    async def _submit(self, text: str):
        command_text = text.strip()
        if not command_text:
            return
        # Slash / builtin commands go through the runtime, result printed above.
        command_result = await self.runtime.handle_command(command_text)
        if command_result is not None:
            await self._print_above(str(command_result))
            await self._refresh_mode()
            return
        bar = theme.sgr(theme.USER_BARCH, theme.USER_BAR)
        await self._print_above(f"\n{bar} {theme.sgr(text, theme.USER_MSG)}")
        result = await self._run_turn(text)
        await self._refresh_mode()
        return result

    async def _run_turn(self, text: str):
        """Drive one message turn, offering the inline confirm callback if the
        runtime accepts one. Falls back cleanly on runtimes that don't (yet)
        take a ``confirm`` kwarg — see BACKEND_REQUIREMENTS.md."""
        if self._runtime_accepts_confirm():
            return await self.runtime.run_message_events(
                text, event_sink=self.renderer, confirm=self.confirm_tool
            )
        return await self.runtime.run_message_events(text, event_sink=self.renderer)

    def _runtime_accepts_confirm(self) -> bool:
        try:
            import inspect

            sig = inspect.signature(self.runtime.run_message_events)
        except (TypeError, ValueError):
            return False
        params = sig.parameters
        if "confirm" in params:
            return True
        return any(p.kind is p.VAR_KEYWORD for p in params.values())

    _MODE_CYCLE = ("Read Only", "Ask First", "Edit Freely", "Full Auto")

    async def _cycle_mode(self) -> None:
        """Advance to the next execution mode via the /mode command path."""
        try:
            idx = self._MODE_CYCLE.index(self.state.exec_mode)
        except ValueError:
            idx = -1
        nxt = self._MODE_CYCLE[(idx + 1) % len(self._MODE_CYCLE)]
        result = await self.runtime.handle_command(f"/mode {nxt}")
        if result is not None:
            await self._print_above(str(result))
        await self._refresh_mode()

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
        command_text = text.strip()
        if command_text in ("exit", "quit", "/exit", "/quit"):
            self.input_area.text = ""
            if self.app is not None:
                self.app.exit()
            return
        if self._turn_task and not self._turn_task.done():
            return  # keep the draft intact while a turn is already running
        self.input_area.text = ""
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

        @kb.add("y", filter=Condition(lambda: self._confirm_future is not None))
        def _(event) -> None:
            self._resolve_confirm("allow")

        @kb.add("n", filter=Condition(lambda: self._confirm_future is not None))
        def _(event) -> None:
            self._resolve_confirm("deny")

        @kb.add("a", filter=Condition(lambda: self._confirm_future is not None))
        def _(event) -> None:
            self._resolve_confirm("allow")

        @kb.add("A", filter=Condition(lambda: self._confirm_future is not None))
        def _(event) -> None:
            self._resolve_confirm("always")

        @kb.add("escape", filter=Condition(lambda: self._confirm_future is not None))
        def _(event) -> None:
            self._resolve_confirm("deny")

        @kb.add("enter")
        def _(event) -> None:
            # While a tool confirmation is pending, Enter uses the visible
            # default action. Today that defaults to allow for compatibility;
            # newer backends can send default_action=deny/none.
            if self._confirm_future is not None:
                default = "allow"
                if self.state.pending_confirm is not None:
                    default = self.state.pending_confirm.default_action
                if default == "deny":
                    self._resolve_confirm("deny")
                elif default == "allow":
                    self._resolve_confirm("allow")
                return
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
            if self._confirm_future is not None:
                self._resolve_confirm("deny")
            elif self._turn_task and not self._turn_task.done():
                asyncio.ensure_future(self._stop())
            else:
                self.input_area.text = ""

        @kb.add("c-o")
        def _(event) -> None:
            self._expand_last()

        @kb.add("s-tab")
        def _(event) -> None:
            # Cycle execution mode (CC habit); no-op if a turn is running.
            if self._turn_task and not self._turn_task.done():
                return
            asyncio.ensure_future(self._cycle_mode())

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
        header = theme.sgr(f"$ {name}", theme.EXPAND_HEADER)
        body = "\n".join(f"  {line}" for line in (full.splitlines() or [full]))
        self._print_above_nowait(f"{header}\n{body}")

    async def confirm_tool(self, decision) -> str:
        """Prompt the user to allow/deny a tool, resolving to 'allow'|'deny'|'always'.

        Passed to the backend as the ``confirm`` callback. ``decision`` is a
        ToolDecision-like object; tolerate current minimal fields and future
        richer display fields from FRONTEND_INTERFACE_REQUIREMENTS.md.
        """
        prompt = self._build_confirm_prompt(decision)

        loop = asyncio.get_running_loop()
        self._confirm_future = loop.create_future()
        self.state.pending_confirm = prompt
        self._invalidate()
        try:
            answer = await self._confirm_future
        finally:
            self.state.pending_confirm = None
            self._confirm_future = None
            self._invalidate()
        return answer

    def _build_confirm_prompt(self, decision) -> ConfirmPrompt:
        name = _decision_field(decision, "display_name") or _decision_field(decision, "tool_name") or "tool"
        category = _decision_field(decision, "permission_category") or ""
        mode = _decision_field(decision, "execution_mode_label") or _decision_field(decision, "execution_mode")
        risk = _decision_field(decision, "risk_summary") or _decision_field(decision, "decision_message")
        default_action = (_decision_field(decision, "default_action") or "allow").lower()
        if default_action not in {"allow", "deny", "none"}:
            default_action = "allow"
        preview = (
            _decision_field(decision, "input_preview")
            or _decision_field(decision, "command_preview")
            or _decision_field(decision, "diff_summary")
            or _decision_field(decision, "input_summary")
        )
        paths = _decision_list(decision, "affected_paths")
        if paths:
            preview = (preview + " · " if preview else "") + ", ".join(paths[:3])

        return ConfirmPrompt(
            title="需要确认",
            display_name=name,
            permission_category=category,
            execution_mode=mode,
            risk_summary=risk,
            input_preview=preview,
            default_action=default_action,
        )

    def _resolve_confirm(self, answer: str) -> None:
        self.input_area.text = ""
        fut = self._confirm_future
        if fut is not None and not fut.done():
            fut.set_result(answer)

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
