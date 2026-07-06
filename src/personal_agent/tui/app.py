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
import unicodedata
from typing import Callable, NamedTuple

from prompt_toolkit.application import Application
from prompt_toolkit.application.run_in_terminal import run_in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout

from personal_agent.tui.layout import build_layout
from personal_agent.tui.renderer import InlineRenderer
from personal_agent.tui.state import ConfirmPrompt, SlashMenuItem, UIState
from personal_agent.tui import theme


class _PrintRequest(NamedTuple):
    text: str | None
    done: asyncio.Future[None] | None


class _SlashCommand(NamedTuple):
    text: str
    description: str = ""
    children: tuple["_SlashCommand", ...] = ()
    arguments: tuple["_SlashArgument", ...] = ()


class _SlashArgumentChoice(NamedTuple):
    value: str
    label: str = ""
    description: str = ""
    append_space: bool = False


class _SlashArgument(NamedTuple):
    name: str
    kind: str
    choices: tuple[_SlashArgumentChoice, ...] = ()
    provider: str = ""
    required: bool = True


class _DynamicChoiceRequest(NamedTuple):
    provider: str
    command: str
    args: tuple[str, ...]
    query: str


class _InlineSlashCompleter(Completer):
    def __init__(self, items_for_text: Callable[[str], tuple[SlashMenuItem, ...]]) -> None:
        self._items_for_text = items_for_text

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        for item in self._items_for_text(text):
            yield Completion(
                item.text,
                start_position=-len(text),
                display=item.display_text or item.text,
                display_meta=item.description,
            )


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


def _command_continue_text(result) -> str | None:
    value = getattr(result, "continue_text", None)
    if value:
        return str(value)
    if isinstance(result, dict) and result.get("continue_text"):
        return str(result["continue_text"])
    return None


def _command_response_text(result) -> str:
    value = getattr(result, "response", None)
    if value is not None:
        return str(value)
    if isinstance(result, dict) and result.get("response") is not None:
        return str(result["response"])
    return str(result)


def _command_kind(result) -> str:
    value = getattr(result, "kind", "")
    if value:
        return str(value)
    if isinstance(result, dict) and result.get("kind"):
        return str(result["kind"])
    return ""


def _command_payload(result) -> dict | None:
    value = getattr(result, "payload", None)
    if isinstance(value, dict):
        return value
    if isinstance(result, dict) and isinstance(result.get("payload"), dict):
        return result["payload"]
    return None


def _slash_command_from_dict(item: object) -> _SlashCommand | None:
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    text = f"/{name.lstrip('/')}"
    description = str(item.get("summary") or "")
    children = tuple(
        child for child in (
            _slash_child_from_dict(text, value)
            for value in item.get("children", [])
        ) if child is not None
    )
    return _SlashCommand(
        text=text,
        description=description,
        children=children,
        arguments=_slash_arguments_from_dict(item),
    )


def _slash_child_from_dict(parent: str, item: object) -> _SlashCommand | None:
    if not isinstance(item, dict):
        return None
    usage = str(item.get("usage") or "").strip()
    name = str(item.get("name") or "").strip()
    text = _completion_text_from_usage(usage) if usage else ""
    if not text and name:
        text = f"{parent} {name.lstrip('/')}"
    if not text:
        return None
    return _SlashCommand(
        text=text,
        description=str(item.get("summary") or ""),
        arguments=_slash_arguments_from_dict(item),
    )


def _slash_arguments_from_dict(item: dict) -> tuple[_SlashArgument, ...]:
    arguments: list[_SlashArgument] = []
    for raw in item.get("arguments", []) or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        kind = str(raw.get("kind") or "").strip()
        if not name or not kind:
            continue
        choices = tuple(
            choice for choice in (
                _slash_argument_choice_from_dict(value)
                for value in raw.get("choices", [])
            ) if choice is not None
        )
        arguments.append(_SlashArgument(
            name=name,
            kind=kind,
            choices=choices,
            provider=str(raw.get("provider") or ""),
            required=bool(raw.get("required", True)),
        ))
    return tuple(arguments)


def _slash_argument_choice_from_dict(item: object) -> _SlashArgumentChoice | None:
    if isinstance(item, str):
        value = item.strip()
        return _SlashArgumentChoice(value=value, label=value) if value else None
    if not isinstance(item, dict):
        return None
    value = str(item.get("value") or "").strip()
    if not value:
        return None
    return _SlashArgumentChoice(
        value=value,
        label=str(item.get("label") or value),
        description=str(item.get("description") or ""),
        append_space=bool(item.get("append_space", False)),
    )


def _completion_text_from_usage(usage: str) -> str:
    parts: list[str] = []
    for token in usage.split():
        if token.startswith("<") or token.startswith("["):
            break
        parts.append(token)
    return " ".join(parts)


def _format_tool_runs_payload(payload: dict) -> tuple[str, tuple[str, str] | None]:
    action = str(payload.get("action") or "recent")
    if action == "summary":
        return _format_tool_runs_summary(payload), None
    if action == "show":
        return _format_tool_run_detail(payload.get("tool_run") or {})
    return _format_tool_runs_recent(payload), None


def _format_tool_runs_recent(payload: dict) -> str:
    items = [item for item in payload.get("items") or [] if isinstance(item, dict)]
    scope = _tool_run_scope(payload)
    if not items:
        return f"Tool runs: none ({scope})"
    lines = [
        theme.sgr(f"Tool runs ({scope})", theme.EXPAND_HEADER),
        theme.sgr("  id   tool          status      time   summary", theme.SLASH_META),
    ]
    for item in items:
        run_id = _field(item, "id") or "-"
        tool = _fit(_field(item, "tool_name") or "-", 12)
        status = _fit(_field(item, "status") or "-", 10)
        duration = _format_duration(item.get("duration"))
        summary = _field(item, "output_summary") or _field(item, "error") or ""
        lines.append(f"  {str(run_id).rjust(3)}  {tool}  {status}  {duration.rjust(5)}  {_fit(summary, 70)}")
    lines.append(theme.sgr("  /tool-runs show <id>", theme.KEY) + theme.sgr(" opens detail", theme.HINT_LABEL))
    return "\n".join(lines)


def _format_tool_runs_summary(payload: dict) -> str:
    scope = _tool_run_scope(payload)
    lines = [
        theme.sgr(f"Tool run summary ({scope})", theme.EXPAND_HEADER),
        f"  inspected {int(payload.get('inspected') or 0)}"
        f"   denied {int(payload.get('denied') or 0)}"
        f"   failed {int(payload.get('failed') or 0)}"
        f"   timeouts {int(payload.get('timeouts') or 0)}"
        f"   truncated {int(payload.get('truncated') or 0)}",
    ]
    for label, key in (
        ("tools", "tool_counts"),
        ("status", "status_counts"),
        ("category", "category_counts"),
    ):
        lines.append(f"  {label}: {_format_counts(payload.get(key))}")
    return "\n".join(lines)


def _format_tool_run_detail(item: dict) -> tuple[str, tuple[str, str] | None]:
    if not isinstance(item, dict):
        return "Tool run detail: missing", None
    run_id = _field(item, "id") or "-"
    name = _field(item, "tool_name") or "tool"
    output = _field(item, "full_output") or _field(item, "output_summary")
    lines = [
        theme.sgr(f"Tool Run #{run_id}", theme.EXPAND_HEADER),
        f"  tool      {name}",
        f"  status    {_field(item, 'status') or '-'}",
        f"  category  {_field(item, 'category') or '-'}",
        f"  duration  {_format_duration(item.get('duration'))}",
        f"  session   {_field(item, 'session_key') or '-'}",
        f"  turn      {_field(item, 'turn_id') or '-'}",
        f"  mode      {_field(item, 'execution_mode') or '-'}",
        f"  permission {_field(item, 'permission_category') or '-'} / {_field(item, 'permission_decision') or '-'}",
    ]
    input_summary = _field(item, "input_summary")
    if input_summary:
        lines.append(f"  input     {_fit(input_summary, 120)}")
    error = _field(item, "error")
    if error:
        lines.append(f"  error     {_fit(error, 120)}")
    if output:
        lines.append(f"  output    {_fit(output, 160)}")
        lines.append(theme.sgr("  Ctrl+O", theme.KEY) + theme.sgr(" expand full output", theme.HINT_LABEL))
    expandable = (f"tool run #{run_id} {name}", output) if output else None
    return "\n".join(lines), expandable


def _tool_run_scope(payload: dict) -> str:
    if payload.get("scope") == "all":
        return "all sessions"
    return str(payload.get("session_key") or "current session")


def _format_counts(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "-"
    return ", ".join(f"{key}={value[key]}" for key in sorted(value))


def _format_duration(value: object) -> str:
    try:
        return f"{float(value or 0.0):.2f}s"
    except (TypeError, ValueError):
        return "0.00s"


def _field(item: dict, name: str) -> str:
    value = item.get(name)
    return "" if value is None else str(value)


def _fit(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)] + "…"


class InlineTuiApp:
    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.state = UIState()
        self.state.exec_mode = "Ask First"
        self.state.model = getattr(getattr(runtime, "settings", None), "llm_model", "") or ""
        self._slash_commands = self._load_slash_commands()
        self.root, self.input_area = build_layout(
            self.state,
            completer=self._build_completer(),
            history=self._build_history(),
        )
        self.input_area.buffer.on_text_changed += self._on_input_text_changed
        self.renderer = InlineRenderer(
            state=self.state,
            invalidate=self._invalidate,
            print_above=self._print_above,
            width=self._term_width(),
        )
        self._turn_task: asyncio.Task | None = None
        self._last_expanded: tuple[str, str] | None = None
        self._confirm_future: asyncio.Future[str] | None = None
        self._slash_choice_cache: dict[_DynamicChoiceRequest, tuple[_SlashArgumentChoice, ...]] = {}
        self._slash_choice_tasks: dict[_DynamicChoiceRequest, asyncio.Task] = {}
        self._print_queue: asyncio.Queue[_PrintRequest] = asyncio.Queue()
        self._print_worker_task: asyncio.Task | None = None
        self.app: Application | None = None

    # ── command registry completer + history ──
    def _build_completer(self):
        if not self._slash_commands:
            return None
        return _InlineSlashCompleter(self._slash_menu_items)

    def _load_slash_commands(self) -> tuple[_SlashCommand, ...]:
        try:
            from personal_agent.commands.registry import command_specs_as_dict

            data = command_specs_as_dict(self.runtime)
        except Exception:
            return ()

        commands: list[_SlashCommand] = []
        for item in data.get("commands", []):
            command = _slash_command_from_dict(item)
            if command is not None:
                commands.append(command)
        for item in data.get("plugin_commands", []):
            command = _slash_command_from_dict(item)
            if command is not None:
                commands.append(command)
        return tuple(commands)

    def _build_history(self) -> History:
        try:
            data_dir = Path(getattr(self.runtime.settings, "agent_data_dir", "data"))
            data_dir.mkdir(parents=True, exist_ok=True)
            return FileHistory(str(data_dir / "cli_history.txt"))
        except Exception:
            return InMemoryHistory()

    def _on_input_text_changed(self, _buffer) -> None:
        text = self.input_area.text
        slash_mode = text.startswith("/") and "\n" not in text
        slash_items = self._slash_menu_items(text) if slash_mode else ()
        if self.state.slash_mode != slash_mode or self.state.slash_items != slash_items:
            self.state.slash_mode = slash_mode
            self.state.slash_items = slash_items
            self._invalidate()

    def _slash_menu_items(self, text: str) -> tuple[SlashMenuItem, ...]:
        return tuple(
            SlashMenuItem(
                text=item.text,
                description=item.description,
                display_text=item.display_text,
            )
            for item in self._slash_candidates(text)
        )

    def _slash_candidates(self, text: str) -> tuple[SlashMenuItem, ...]:
        if not text.startswith("/") or "\n" in text:
            return ()
        query = text.strip()
        if query in ("", "/"):
            return self._command_menu_items(self._slash_commands)

        tokens = query.split()
        command_text = tokens[0] if tokens else query
        trailing_space = text.endswith(" ")
        command = self._find_slash_command(command_text)

        if len(tokens) <= 1 and not trailing_space:
            matches = tuple(item for item in self._slash_commands if item.text.startswith(command_text))
            if command is not None and len(matches) == 1:
                if command.children:
                    return self._command_menu_items(command.children)
                return self._argument_menu_items(command, text, tokens, trailing_space)
            return self._command_menu_items(matches)

        if command is None or not command.children:
            return self._argument_menu_items(command, text, tokens, trailing_space) if command else ()

        prefix = f"{command_text} " if len(tokens) == 1 and trailing_space else query
        matches = tuple(item for item in command.children if item.text.startswith(prefix))
        if (
            len(matches) == 1
            and matches[0].text == query
            and not matches[0].children
        ):
            return self._argument_menu_items(matches[0], text, tokens, trailing_space)
        if matches:
            return self._command_menu_items(matches)
        child = self._find_child_command(command, tokens)
        return self._argument_menu_items(child, text, tokens, trailing_space) if child else ()

    def _find_slash_command(self, text: str) -> _SlashCommand | None:
        for item in self._slash_commands:
            if item.text == text:
                return item
        return None

    def _find_child_command(self, command: _SlashCommand, tokens: list[str]) -> _SlashCommand | None:
        if len(tokens) < 2:
            return None
        child_text = " ".join(tokens[:2])
        for item in command.children:
            if item.text == child_text:
                return item
        return None

    def _command_menu_items(self, commands: tuple[_SlashCommand, ...]) -> tuple[SlashMenuItem, ...]:
        return tuple(
            SlashMenuItem(text=item.text, description=item.description)
            for item in commands
        )

    def _argument_menu_items(
        self,
        command: _SlashCommand,
        text: str,
        tokens: list[str],
        trailing_space: bool,
    ) -> tuple[SlashMenuItem, ...]:
        if not command.arguments:
            return ()
        command_tokens = command.text.split()
        if len(tokens) < len(command_tokens):
            return ()
        if tokens[:len(command_tokens)] != command_tokens:
            return ()
        arg_index = max(0, len(tokens) - len(command_tokens) - (0 if trailing_space else 1))
        if arg_index >= len(command.arguments):
            return ()
        argument = command.arguments[arg_index]
        prefix = "" if trailing_space or len(tokens) == len(command_tokens) else tokens[-1]
        base = command.text if trailing_space or len(tokens) == len(command_tokens) else " ".join(tokens[:-1])
        existing_args = tokens[len(command_tokens):]
        if prefix and existing_args:
            existing_args = existing_args[:-1]
        choices = self._argument_choices(
            argument,
            command,
            prefix=prefix,
            existing_args=tuple(existing_args),
        )
        if not choices:
            return ()
        candidates: list[SlashMenuItem] = []
        for choice in choices:
            if prefix and not choice.value.lower().startswith(prefix.lower()):
                continue
            completed = f"{base} {choice.value}".strip()
            if choice.append_space:
                completed += " "
            if completed == text.strip() and not choice.append_space:
                continue
            candidates.append(SlashMenuItem(
                text=completed,
                display_text=choice.label or choice.value,
                description=choice.description,
            ))
        return tuple(candidates)

    def _argument_choices(
        self,
        argument: _SlashArgument,
        command: _SlashCommand,
        *,
        prefix: str,
        existing_args: tuple[str, ...],
    ) -> tuple[_SlashArgumentChoice, ...]:
        if argument.kind == "choice":
            return argument.choices
        if argument.kind != "dynamic" or not argument.provider:
            return ()
        command_parts = command.text.lstrip("/").split()
        if not command_parts:
            return ()
        request = _DynamicChoiceRequest(
            provider=argument.provider,
            command=command_parts[0],
            args=tuple(command_parts[1:]) + existing_args,
            query=prefix,
        )
        cached = self._slash_choice_cache.get(request)
        if cached is not None:
            return cached
        self._ensure_dynamic_choice_task(request)
        return ()

    def _ensure_dynamic_choice_task(self, request: _DynamicChoiceRequest) -> None:
        task = self._slash_choice_tasks.get(request)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._slash_choice_tasks[request] = loop.create_task(self._load_dynamic_choices(request))

    async def _load_dynamic_choices(self, request: _DynamicChoiceRequest) -> None:
        try:
            from personal_agent.commands.runtime import slash_argument_choices

            raw_choices = await slash_argument_choices(
                self.runtime,
                request.provider,
                command=request.command,
                args=request.args,
                query=request.query,
                limit=20,
            )
            choices = tuple(
                choice for choice in (
                    _slash_argument_choice_from_dict(value)
                    for value in raw_choices
                ) if choice is not None
            )
            self._slash_choice_cache[request] = choices
        except Exception:
            self._slash_choice_cache[request] = ()
        finally:
            self._slash_choice_tasks.pop(request, None)
        if self.state.slash_mode:
            text = self.input_area.text
            slash_items = self._slash_menu_items(text)
            if self.state.slash_items != slash_items:
                self.state.slash_items = slash_items
                self._invalidate()

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
        command_result = await self._handle_command(command_text)
        if command_result is not None:
            continue_text = _command_continue_text(command_result)
            if continue_text is not None:
                await self._print_above(self._user_message_block(continue_text))
                result = await self._run_turn(continue_text)
                await self._refresh_mode()
                return result
            response = self._command_output(command_result)
            if response:
                await self._print_above(response)
            await self._refresh_mode()
            return
        await self._print_above(self._user_message_block(text))
        result = await self._run_turn(text)
        await self._refresh_mode()
        return result

    async def _handle_command(self, text: str):
        if text.startswith("/"):
            try:
                from personal_agent.commands.runtime import handle_slash_command

                result = await handle_slash_command(self.runtime, text)
            except Exception:
                result = None
            else:
                if getattr(result, "handled", False):
                    return result
        handler = getattr(self.runtime, "handle_command", None)
        if handler is None:
            return None
        return await handler(text)

    def _command_output(self, result) -> str:
        if _command_kind(result) == "tool_runs":
            payload = _command_payload(result)
            if payload and not payload.get("error"):
                text, expandable = _format_tool_runs_payload(payload)
                if expandable is not None:
                    self.state.last_expandable = expandable
                    self._invalidate()
                return text
        return _command_response_text(result)

    def _user_message_block(self, text: str) -> str:
        width = max(20, self._term_width())
        prefix = f"{theme.USER_BARCH} "
        rows: list[str] = [""]
        for index, line in enumerate(text.splitlines() or [text]):
            raw_prefix = prefix if index == 0 else "  "
            visible = f"{raw_prefix}{line}"
            pad = " " * max(0, width - _display_width(visible))
            styled_prefix = (
                theme.sgr(theme.USER_BARCH, theme.USER_BAR) + theme.sgr(" ", theme.USER_MSG)
                if index == 0
                else theme.sgr(raw_prefix, theme.USER_MSG)
            )
            rows.append(styled_prefix + theme.sgr(line + pad, theme.USER_MSG))
        return "\n".join(rows)

    async def _run_turn(self, text: str):
        """Drive one message turn, offering the inline confirm callback if the
        runtime accepts one. Falls back cleanly on runtimes that don't (yet)
        take a ``confirm`` kwarg — see BACKEND_INTERFACE.md."""
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
            self._resolve_confirm_action("allow_once")

        @kb.add("n", filter=Condition(lambda: self._confirm_future is not None))
        def _(event) -> None:
            self._resolve_confirm("deny")

        @kb.add("a", filter=Condition(lambda: self._confirm_future is not None))
        def _(event) -> None:
            self._resolve_confirm_action("allow_once")

        @kb.add("A", filter=Condition(lambda: self._confirm_future is not None))
        def _(event) -> None:
            self._resolve_confirm_action("allow_always")

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
                    self._resolve_confirm_action("allow_once")
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
        header = theme.sgr(f"展开 {name}", theme.EXPAND_HEADER)
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
        risk_level = _decision_field(decision, "risk_level") or ""
        default_action = (_decision_field(decision, "default_action") or "allow").lower()
        if default_action not in {"allow", "deny", "none"}:
            default_action = "allow"
        actions = tuple(_decision_list(decision, "available_actions"))
        if not actions:
            actions = ("allow_once", "allow_always", "deny")
        preview = (
            _decision_field(decision, "input_preview")
            or _decision_field(decision, "command_preview")
            or _decision_field(decision, "diff_summary")
            or _decision_field(decision, "input_summary")
        )
        paths = _decision_list(decision, "affected_paths")
        command_preview = _decision_field(decision, "command_preview")
        url_preview = _decision_field(decision, "url_preview")
        host = _decision_field(decision, "host")
        process_label = _decision_field(decision, "process_label")
        if paths and not (command_preview or url_preview):
            preview = (preview + " · " if preview else "") + ", ".join(paths[:3])

        return ConfirmPrompt(
            title="需要确认",
            display_name=name,
            permission_category=category,
            execution_mode=mode,
            risk_level=risk_level,
            risk_summary=risk,
            input_preview=preview,
            command_preview=command_preview,
            url_preview=url_preview,
            host=host,
            process_label=process_label,
            affected_paths=tuple(paths),
            default_action=default_action,
            available_actions=actions,
        )

    def _resolve_confirm_action(self, action: str) -> None:
        prompt = self.state.pending_confirm
        available = set(prompt.available_actions if prompt is not None else ())
        if action == "allow_once" and (not available or "allow_once" in available):
            self._resolve_confirm("allow")
        elif action == "allow_always" and (not available or "allow_always" in available):
            self._resolve_confirm("always")

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


def _display_width(text: str) -> int:
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width
