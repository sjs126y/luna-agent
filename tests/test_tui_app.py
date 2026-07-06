"""Unit tests for InlineTuiApp wiring (no real terminal).

Covers slash-menu/history wiring, Ctrl+O expand behavior, and the guard that
prevents launching a second turn while one is running.
"""

from __future__ import annotations

import asyncio

import pytest

from personal_agent.commands.runtime import CommandResult
from personal_agent.tui.app import InlineTuiApp


class _Settings:
    llm_model = "deepseek-chat"
    agent_data_dir = "data"


class _Runtime:
    def __init__(self) -> None:
        self.settings = _Settings()
        self.sent: list[str] = []
        self.commands: list[str] = []
        self.plugin_manager = None
        self.plugin_command_scopes = ("slash", "cli")

    async def handle_command(self, text: str):
        self.commands.append(text)
        return None  # not a command

    async def run_message_events(self, text: str, event_sink=None):
        self.sent.append(text)
        return None

    async def get_agent(self):
        return object()

    def plugin_command_kwargs(self, args: str) -> dict:
        return {"args": args, "runtime": self}

    mode = "Ask First"

    async def current_execution_mode(self) -> str:
        return self.mode


def _app() -> InlineTuiApp:
    return InlineTuiApp(_Runtime())


def test_history_wired_and_prompt_toolkit_completer_disabled():
    app = _app()
    assert app.input_area.completer is None
    assert app.input_area.buffer.history is not None


def test_slash_menu_uses_command_registry_metadata():
    app = _app()
    app.input_area.text = "/comm"
    assert any(item.text == "/commands" for item in app.state.slash_items)


def test_slash_menu_offers_child_commands_without_placeholders():
    app = _app()
    app.input_area.text = "/mode "
    texts = {item.text for item in app.state.slash_items}
    assert "/mode list" in texts
    assert "/mode show" in texts
    assert "/mode set" in texts
    assert all("<mode>" not in text for text in texts)


def test_slash_menu_offers_argument_choices_from_registry(monkeypatch):
    def command_specs_as_dict(runtime):
        return {
            "commands": [
                {
                    "name": "mode",
                    "summary": "查看或切换执行模式",
                    "usage": "/mode [set <mode>]",
                    "children": [
                        {
                            "name": "set",
                            "summary": "切换执行模式",
                            "usage": "/mode set <mode>",
                            "arguments": [
                                {
                                    "name": "mode",
                                    "kind": "choice",
                                    "choices": [
                                        {
                                            "value": "Ask First",
                                            "label": "Ask First",
                                            "description": "执行前确认",
                                        },
                                        {
                                            "value": "Full Auto",
                                            "label": "Full Auto",
                                            "description": "全自动",
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
            "plugin_commands": [],
        }

    monkeypatch.setattr(
        "personal_agent.commands.registry.command_specs_as_dict",
        command_specs_as_dict,
    )
    app = _app()
    app.input_area.text = "/mode set "
    assert [
        (item.text, item.display_text, item.description)
        for item in app.state.slash_items
    ] == [
        ("/mode set Ask First", "Ask First", "执行前确认"),
        ("/mode set Full Auto", "Full Auto", "全自动"),
    ]

    app.input_area.text = "/mode set A"
    assert [(item.text, item.display_text) for item in app.state.slash_items] == [
        ("/mode set Ask First", "Ask First")
    ]


@pytest.mark.asyncio
async def test_dynamic_argument_choices_refresh_slash_menu(monkeypatch):
    def command_specs_as_dict(runtime):
        return {
            "commands": [
                {
                    "name": "tools",
                    "summary": "查看可用工具",
                    "usage": "/tools [show <name>]",
                    "children": [
                        {
                            "name": "show",
                            "summary": "查看工具详情",
                            "usage": "/tools show <name>",
                            "arguments": [
                                {
                                    "name": "name",
                                    "kind": "dynamic",
                                    "provider": "tools",
                                }
                            ],
                        }
                    ],
                }
            ],
            "plugin_commands": [],
        }

    calls: list[tuple[str, str, tuple[str, ...], str]] = []

    async def slash_argument_choices(runtime, provider, *, command, args=(), query="", limit=20):
        calls.append((provider, command, tuple(args), query))
        await asyncio.sleep(0)
        return [
            {
                "value": "read",
                "label": "read",
                "description": "Read files",
                "append_space": False,
            }
        ]

    monkeypatch.setattr(
        "personal_agent.commands.registry.command_specs_as_dict",
        command_specs_as_dict,
    )
    monkeypatch.setattr(
        "personal_agent.commands.runtime.slash_argument_choices",
        slash_argument_choices,
    )

    app = _app()
    app.input_area.text = "/tools show r"
    assert app.state.slash_items == ()
    assert app.state.slash_empty_message == ""

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls == [("tools", "tools", ("show",), "r")]
    assert [
        (item.text, item.display_text, item.description)
        for item in app.state.slash_items
    ] == [
        ("/tools show read", "read", "Read files")
    ]


def test_expand_last_noop_when_empty():
    app = _app()
    printed: list[str] = []
    app._print_above_nowait = printed.append  # type: ignore[method-assign]
    app._expand_last()
    assert printed == []


def test_expand_last_prints_when_present():
    app = _app()
    printed: list[str] = []
    app._print_above_nowait = printed.append  # type: ignore[method-assign]
    app.state.last_expandable = ("read", "line1\nline2")
    app._expand_last()
    assert len(printed) == 1
    assert "read" in printed[0]
    assert "──" in printed[0]
    assert "line1" in printed[0] and "line2" in printed[0]


def test_expand_last_dedups_repeated_presses():
    app = _app()
    printed: list[str] = []
    app._print_above_nowait = printed.append  # type: ignore[method-assign]
    app.state.last_expandable = ("read", "data")
    app._expand_last()
    app._expand_last()
    app._expand_last()
    assert len(printed) == 1  # same content only printed once


@pytest.mark.asyncio
async def test_print_above_serializes_concurrent_callers(monkeypatch):
    # Reproduces the parallel-tools race: several callers enqueue lines at the
    # same time. One print worker must flush them without overlap or loss.
    app = _app()
    active = {"n": 0, "max": 0}
    done: list[int] = []
    written: list[str] = []

    async def fake_run_in_terminal(func):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        await asyncio.sleep(0)  # yield: overlap would show up here
        func()
        active["n"] -= 1

    monkeypatch.setattr("personal_agent.tui.app.run_in_terminal", fake_run_in_terminal)
    app._write_terminal = written.append  # type: ignore[method-assign]

    async def one(i: int) -> None:
        await app._print_above(str(i))
        done.append(i)

    await asyncio.gather(*(one(i) for i in range(5)))
    await app._stop_print_worker()
    assert active["max"] == 1  # never more than one in the terminal at once
    assert len(done) == 5  # none dropped
    text = "".join(written)
    for i in range(5):
        assert f"{i}\n" in text


@pytest.mark.asyncio
async def test_print_above_nowait_uses_same_queue(monkeypatch):
    app = _app()
    written: list[str] = []

    async def fake_run_in_terminal(func):
        func()

    monkeypatch.setattr("personal_agent.tui.app.run_in_terminal", fake_run_in_terminal)
    app._write_terminal = written.append  # type: ignore[method-assign]

    app._print_above_nowait("expand")
    await app._stop_print_worker()
    assert written == ["expand\n"]


@pytest.mark.asyncio
async def test_submit_routes_message_to_runtime():
    app = _app()
    printed: list[str] = []

    async def print_above(text):
        printed.append(text)

    app._print_above = print_above  # type: ignore[method-assign]
    await app._submit("hello")
    assert app.runtime.sent == ["hello"]
    # user line echoed above
    assert any("hello" in line for line in printed)


@pytest.mark.asyncio
async def test_submit_echoes_user_message_with_background():
    app = _app()
    printed: list[str] = []

    async def print_above(text):
        printed.append(text)

    app._print_above = print_above  # type: ignore[method-assign]
    await app._submit("现在试一下？")
    text = "\n".join(printed)
    assert "现在试一下？" in text
    assert "\x1b[1;37;48;5;236m" in text


@pytest.mark.asyncio
async def test_submit_preserves_message_body_whitespace():
    app = _app()

    async def print_above(text):
        pass

    app._print_above = print_above  # type: ignore[method-assign]
    await app._submit("  keep\n  indentation  ")
    assert app.runtime.sent == ["  keep\n  indentation  "]
    assert app.runtime.commands == ["keep\n  indentation"]


@pytest.mark.asyncio
async def test_submit_ignores_blank():
    app = _app()
    await app._submit("   ")
    assert app.runtime.sent == []


@pytest.mark.asyncio
async def test_refresh_mode_updates_status_after_command():
    app = _app()

    async def print_above(text):
        pass

    app._print_above = print_above  # type: ignore[method-assign]

    async def handle_command(text):
        app.runtime.mode = "Full Auto"
        return "执行模式已切换: Full Auto"

    app.runtime.handle_command = handle_command  # type: ignore[method-assign]
    assert app.state.exec_mode == "Ask First"
    await app._submit("/mode Full Auto")
    assert app.state.exec_mode == "Full Auto"


@pytest.mark.asyncio
async def test_cycle_mode_advances_and_wraps():
    app = _app()
    printed: list[str] = []

    async def print_above(text):
        printed.append(text)

    app._print_above = print_above  # type: ignore[method-assign]

    async def handle_command(text):
        # emulate backend: /mode X sets the runtime's reported mode
        app.runtime.mode = text.removeprefix("/mode ").strip()
        return f"执行模式已切换: {app.runtime.mode}"

    app.runtime.handle_command = handle_command  # type: ignore[method-assign]

    assert app.state.exec_mode == "Ask First"
    await app._cycle_mode()
    assert app.state.exec_mode == "Edit Freely"
    await app._cycle_mode()
    assert app.state.exec_mode == "Full Auto"
    await app._cycle_mode()
    assert app.state.exec_mode == "Read Only"
    await app._cycle_mode()
    assert app.state.exec_mode == "Ask First"  # wraps


@pytest.mark.asyncio
async def test_confirm_tool_resolves_allow():
    app = _app()
    task = asyncio.ensure_future(app.confirm_tool({
        "tool_name": "write_file",
        "permission_category": "write",
        "risk_summary": "将写入文件",
        "input_preview": "src/app.py",
    }))
    await asyncio.sleep(0)  # let confirm_tool set pending + create the future
    assert app.state.pending_confirm is not None
    assert app.state.pending_confirm.display_name == "write_file"
    assert app.state.pending_confirm.risk_summary == "将写入文件"
    assert app.state.pending_confirm.input_preview == "src/app.py"
    app.input_area.text = "y"
    app._resolve_confirm("allow")
    assert await task == "allow"
    # cleaned up after resolution
    assert app.state.pending_confirm is None
    assert app._confirm_future is None
    assert app.input_area.text == ""


@pytest.mark.asyncio
async def test_confirm_tool_resolves_deny_and_always():
    app = _app()
    task = asyncio.ensure_future(app.confirm_tool({"tool_name": "bash"}))
    await asyncio.sleep(0)
    app._resolve_confirm("deny")
    assert await task == "deny"

    task = asyncio.ensure_future(app.confirm_tool({"tool_name": "bash"}))
    await asyncio.sleep(0)
    app._resolve_confirm("always")
    assert await task == "always"


def test_confirm_prompt_uses_future_display_fields():
    app = _app()
    prompt = app._build_confirm_prompt({
        "display_name": "Run shell command",
        "tool_name": "bash",
        "permission_category": "bash",
        "execution_mode_label": "Ask First",
        "default_action": "deny",
        "available_actions": ["deny"],
        "command_preview": "rm -rf build",
        "affected_paths": ["build"],
    })
    assert prompt.display_name == "Run shell command"
    assert prompt.permission_category == "bash"
    assert prompt.execution_mode == "Ask First"
    assert prompt.default_action == "deny"
    assert prompt.available_actions == ("deny",)
    assert "rm -rf build" in prompt.input_preview
    assert prompt.command_preview == "rm -rf build"
    assert prompt.affected_paths == ("build",)
    assert "build" in prompt.input_preview


def test_confirm_prompt_preserves_network_display_fields():
    app = _app()
    prompt = app._build_confirm_prompt({
        "display_name": "Fetch URL",
        "tool_name": "web_fetch",
        "url_preview": "https://example.test/a",
        "host": "example.test",
        "input_preview": "https://example.test/a",
    })
    assert prompt.url_preview == "https://example.test/a"
    assert prompt.host == "example.test"


@pytest.mark.asyncio
async def test_confirm_action_respects_available_actions():
    app = _app()
    fut = asyncio.get_running_loop().create_future()
    app._confirm_future = fut
    app.state.pending_confirm = app._build_confirm_prompt({
        "tool_name": "bash",
        "available_actions": ["deny"],
    })
    app._resolve_confirm_action("allow_once")
    assert fut.done() is False
    app._resolve_confirm("deny")


def test_runtime_accepts_confirm_detection():
    app = _app()
    # fake runtime's run_message_events is (text, event_sink=None): no confirm
    assert app._runtime_accepts_confirm() is False

    async def with_confirm(text, event_sink=None, confirm=None):
        return None

    app.runtime.run_message_events = with_confirm  # type: ignore[method-assign]
    assert app._runtime_accepts_confirm() is True

    async def with_kwargs(text, event_sink=None, **kw):
        return None

    app.runtime.run_message_events = with_kwargs  # type: ignore[method-assign]
    assert app._runtime_accepts_confirm() is True


def test_enter_keeps_draft_while_turn_running():
    app = _app()
    class _RunningTask:
        def done(self) -> bool:
            return False

    app._turn_task = _RunningTask()  # type: ignore[assignment]
    app.input_area.text = "next question"
    app._on_enter()
    assert app.input_area.text == "next question"


@pytest.mark.asyncio
async def test_command_not_sent_as_message():
    app = _app()

    printed: list[str] = []

    async def print_above(text):
        printed.append(text)

    app._print_above = print_above  # type: ignore[method-assign]
    await app._submit("/help")
    assert app.runtime.sent == []  # not routed as a message
    assert any("/commands" in line for line in printed)


@pytest.mark.asyncio
async def test_command_falls_back_to_runtime_handler_for_non_slash():
    app = _app()
    printed: list[str] = []

    async def handle_command(text):
        return "command output"

    async def print_above(text):
        printed.append(text)

    app.runtime.handle_command = handle_command  # type: ignore[method-assign]
    app._print_above = print_above  # type: ignore[method-assign]

    await app._submit("custom")
    assert app.runtime.sent == []
    assert any("command output" in line for line in printed)


@pytest.mark.asyncio
async def test_command_continue_text_routes_through_inline_turn(monkeypatch):
    app = _app()
    printed: list[str] = []

    async def fake_handle_slash_command(runtime, text):
        return CommandResult.continue_with("skill message")

    async def print_above(text):
        printed.append(text)

    monkeypatch.setattr("personal_agent.commands.runtime.handle_slash_command", fake_handle_slash_command)
    app._print_above = print_above  # type: ignore[method-assign]

    await app._submit("/python-expert skill message")
    assert app.runtime.sent == ["skill message"]
    assert any("skill message" in line for line in printed)


@pytest.mark.asyncio
async def test_tool_runs_recent_payload_formats_structured_output(monkeypatch):
    app = _app()
    printed: list[str] = []

    async def fake_handle_slash_command(runtime, text):
        assert text == "/tool-runs"
        return CommandResult.reply(
            "backend fallback text",
            kind="tool_runs",
            payload={
                "action": "recent",
                "scope": "session",
                "session_key": "cli:default:local",
                "items": [
                    {
                        "id": 7,
                        "tool_name": "read",
                        "status": "success",
                        "duration": 0.125,
                        "output_summary": "README contents",
                    }
                ],
            },
        )

    async def print_above(text):
        printed.append(text)

    monkeypatch.setattr("personal_agent.commands.runtime.handle_slash_command", fake_handle_slash_command)
    app._print_above = print_above  # type: ignore[method-assign]

    await app._submit("/tool-runs")

    text = "\n".join(printed)
    assert "Tool runs (cli:default:local)" in text
    assert "read" in text
    assert "success" in text
    assert "/tool-runs show <id>" in text
    assert "backend fallback text" not in text


@pytest.mark.asyncio
async def test_tool_run_detail_payload_sets_expandable_output(monkeypatch):
    app = _app()
    printed: list[str] = []

    async def fake_handle_slash_command(runtime, text):
        assert text == "/tool-runs show 7"
        return CommandResult.reply(
            "backend fallback text",
            kind="tool_runs",
            payload={
                "action": "show",
                "tool_run": {
                    "id": 7,
                    "tool_name": "read",
                    "status": "success",
                    "category": "read",
                    "duration": 0.2,
                    "session_key": "cli:default:local",
                    "turn_id": "turn-1",
                    "execution_mode": "standard",
                    "permission_category": "read",
                    "permission_decision": "allow",
                    "input_summary": '{"path": "README.md"}',
                    "output_summary": "short",
                    "full_output": "line1\nline2",
                },
            },
        )

    async def print_above(text):
        printed.append(text)

    monkeypatch.setattr("personal_agent.commands.runtime.handle_slash_command", fake_handle_slash_command)
    app._print_above = print_above  # type: ignore[method-assign]

    await app._submit("/tool-runs show 7")

    text = "\n".join(printed)
    assert "Tool Run #7" in text
    assert "Path README.md" in text
    assert "Ctrl+O" in text
    assert app.state.last_expandable == ("read #7", "line1\nline2")


def test_slash_mode_tracks_input_text():
    app = _app()
    assert app.state.slash_mode is False
    app.input_area.text = "/"
    assert app.state.slash_mode is True
    assert app.state.slash_items
    app.input_area.text = "/he"
    assert app.state.slash_mode is True
    assert any(item.text == "/help" for item in app.state.slash_items)
    app.input_area.text = "/he\nsecond"
    assert app.state.slash_mode is False
    assert app.state.slash_items == ()
    app.input_area.text = "hello"
    assert app.state.slash_mode is False
    assert app.state.slash_items == ()


def test_complete_leaf_slash_command_hides_menu_but_stays_in_command_mode():
    app = _app()
    app.input_area.text = "/usage"
    assert app.state.slash_mode is True
    assert app.state.slash_items == ()
    assert app.state.has_slash_menu() is False


def test_unknown_slash_command_shows_no_matches():
    app = _app()
    app.input_area.text = "/definitely-not-a-command"
    assert app.state.slash_mode is True
    assert app.state.slash_items == ()
    assert app.state.slash_empty_message == "No matches"
    assert app.state.has_slash_menu() is True


def test_partial_slash_command_shows_matching_candidates():
    app = _app()
    app.input_area.text = "/a"
    assert [item.text for item in app.state.slash_items] == ["/allow", "/agents"]


def test_parent_slash_command_shows_child_candidates():
    app = _app()
    app.input_area.text = "/mode"
    texts = [item.text for item in app.state.slash_items]
    assert "/mode list" in texts
    assert "/mode show" in texts
    assert "/mode set" in texts


def test_root_parent_command_enters_child_menu():
    app = _app()
    app.input_area.text = "/s"
    texts = [item.text for item in app.state.slash_items]
    app._move_slash_selection(texts.index("/session"))

    assert app._apply_selected_slash_item() is True
    assert app.input_area.text == "/session"
    assert [item.text for item in app.state.slash_items] == [
        "/session current",
        "/session list",
        "/session switch",
        "/session rename",
        "/session delete",
    ]


def test_mode_menu_apply_preserves_full_command_text():
    app = _app()
    app.input_area.text = "/mode"
    texts = [item.text for item in app.state.slash_items]
    app._move_slash_selection(texts.index("/mode set"))

    assert app._apply_selected_slash_item() is True
    assert app.input_area.text == "/mode set"
    assert "/mde" not in app.input_area.text


def test_slash_menu_selection_moves_and_applies_current_candidate():
    app = _app()
    app.input_area.text = "/memory"

    assert [item.text for item in app.state.slash_items][:2] == [
        "/memory doctor",
        "/memory list",
    ]
    app._move_slash_selection(1)
    assert app.state.slash_selected == 1

    assert app._apply_selected_slash_item() is True
    assert app.input_area.text == "/memory list"
    assert app.state.slash_mode is True
    assert app.state.slash_items == ()


def test_clear_slash_input_closes_menu():
    app = _app()
    app.input_area.text = "/a"
    assert app.state.has_slash_menu() is True
    app._clear_slash_input()
    assert app.input_area.text == ""
    assert app.state.slash_mode is False
    assert app.state.has_slash_menu() is False


def test_slash_menu_scrolls_when_selection_moves_past_visible_rows():
    app = _app()
    app.input_area.text = "/memory"
    assert len(app.state.slash_items) > 4

    for _ in range(4):
        app._move_slash_selection(1)

    assert app.state.slash_selected == 4
    assert app.state.slash_scroll == 1

    app._move_slash_selection(1)
    assert app.state.slash_selected == 0
    assert app.state.slash_scroll == 0


def test_slash_menu_enter_can_continue_into_argument_choices():
    app = _app()
    app.input_area.text = "/mode"
    texts = [item.text for item in app.state.slash_items]
    app._move_slash_selection(texts.index("/mode set"))

    assert app._apply_selected_slash_item() is True
    assert app.input_area.text == "/mode set"
    assert [
        (item.text, item.display_text)
        for item in app.state.slash_items
    ] == [
        ("/mode set Read Only", "Read Only"),
        ("/mode set Ask First", "Ask First"),
        ("/mode set Edit Freely", "Edit Freely"),
        ("/mode set Full Auto", "Full Auto"),
    ]

    app._move_slash_selection(1)
    assert app._apply_selected_slash_item() is True
    assert app.input_area.text == "/mode set Ask First"
    assert app.state.slash_items == ()
