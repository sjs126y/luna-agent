"""Phase 0 spike: verify prompt_toolkit inline-scroll + bottom active region.

Goal (per TUI_PLAN.md §5 Phase 0): prove this API combo works BEFORE writing
the real tui/ package.

Validates 4 things:
  1. Input box stays pinned to the bottom.
  2. A streaming reply repaints in place in the bottom active region.
  3. Finalized content is printed ABOVE via run_in_terminal() -> enters native
     terminal scrollback (scroll up with mouse/shift-pgup works).
  4. Ctrl+C interrupts the stream; Ctrl+D / empty exit leaves no residue.

Run:  uv run python scripts/spike_inline.py
Type a line + Enter to trigger a fake streamed reply. Ctrl+C stops a running
stream. Ctrl+D (empty input) or /quit exits.
"""

from __future__ import annotations

import asyncio

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.application.run_in_terminal import run_in_terminal
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import TextArea

# ── shared UI state (the real InlineRenderer will own an equivalent) ──
class SpikeState:
    def __init__(self) -> None:
        self.stream_text = ""       # currently-streaming reply (repaints in place)
        self.streaming = False
        self.status = "just now · spike · ready"
        self.turn = 0


state = SpikeState()
stream_task: asyncio.Task | None = None


def active_height() -> int:
    # Active region collapses to 0 lines when idle so only input+status show.
    if not state.streaming and not state.stream_text:
        return 0
    # 1 divider + wrapped-ish text; keep it small for the spike.
    return min(8, 2 + state.stream_text.count("\n") + len(state.stream_text) // 60)


def active_content():
    if not state.stream_text and not state.streaming:
        return ANSI("")
    cursor = "\x1b[36m▌\x1b[0m" if state.streaming else ""
    body = f"\x1b[1;36mAgent:\x1b[0m {state.stream_text}{cursor}"
    return ANSI("\x1b[2m" + "─" * 50 + "\x1b[0m\n" + body)


active_window = Window(
    content=FormattedTextControl(active_content),
    height=Dimension(min=0, weight=1),
    wrap_lines=True,
)

status_window = Window(
    content=FormattedTextControl(lambda: ANSI(f"\x1b[2m{state.status}\x1b[0m")),
    height=1,
)

input_area = TextArea(
    height=1,
    prompt="› ",
    multiline=False,
    wrap_lines=False,
)

root = HSplit([
    ConditionalContainer(active_window, filter=Condition(lambda: active_height() > 0)),
    status_window,
    input_area,
])

kb = KeyBindings()


async def fake_stream(prompt: str) -> None:
    """Simulate token-by-token streaming into the bottom active region."""
    state.turn += 1
    state.streaming = True
    state.stream_text = ""
    words = (
        f"收到「{prompt}」。这是第 {state.turn} 轮的模拟流式回复，"
        "逐字追加，活跃区应当原地重绘而不往下滚动。"
        "完成后这段会被打印到上方进入 scrollback。"
    )
    app = get_app()
    try:
        for ch in words:
            state.stream_text += ch
            app.invalidate()
            await asyncio.sleep(0.02)
    except asyncio.CancelledError:
        # interrupted: finalize what we have with a marker
        await run_in_terminal(
            lambda: print(f"\x1b[1;36mAgent:\x1b[0m {state.stream_text} \x1b[33m[已中断]\x1b[0m")
        )
        raise
    finally:
        state.streaming = False

    final = state.stream_text
    state.stream_text = ""
    # Print finalized reply ABOVE the app -> native scrollback.
    await run_in_terminal(lambda: print(f"\x1b[1;36mAgent:\x1b[0m {final}"))
    state.status = f"turn {state.turn} · spike · ready"
    app.invalidate()


@kb.add("enter")
def _(event) -> None:
    global stream_task
    text = input_area.text.strip()
    input_area.text = ""
    if not text:
        return
    if text in ("/quit", "/exit"):
        event.app.exit()
        return
    # Echo the user line into scrollback, then stream a reply.
    asyncio.ensure_future(run_in_terminal(lambda: print(f"\x1b[1m你:\x1b[0m {text}")))
    if stream_task and not stream_task.done():
        return  # already streaming; ignore
    stream_task = asyncio.ensure_future(fake_stream(text))


@kb.add("c-c")
def _(event) -> None:
    # Ctrl+C: interrupt a running stream, else clear input.
    if stream_task and not stream_task.done():
        stream_task.cancel()
    else:
        input_area.text = ""


@kb.add("c-d")
def _(event) -> None:
    if not input_area.text:
        event.app.exit()


def main() -> None:
    app = Application(
        layout=Layout(root, focused_element=input_area),
        key_bindings=kb,
        full_screen=False,        # KEY: inline, not alternate-screen
        mouse_support=False,      # let terminal own scroll/selection
    )
    print("── spike_inline: 输入文字+Enter 触发模拟流式；Ctrl+C 中断；Ctrl+D 空行退出 ──")
    app.run()
    print("── 已退出。上面的对话应仍在 scrollback 里，且无全屏残留。 ──")


if __name__ == "__main__":
    main()
