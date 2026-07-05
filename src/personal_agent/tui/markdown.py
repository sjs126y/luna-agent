"""Render rich renderables to an ANSI string for prompt_toolkit.

Phase 1 keeps this deliberately simple: rich draws into a string buffer, and we
hand the ANSI-coded text to prompt_toolkit via ``ANSI(...)``. rich no longer
controls the terminal — it is only a markdown -> string formatter now. Richer
formatting (code highlight tuning, tables) is a Phase 3 polish concern.
"""

from __future__ import annotations

from io import StringIO

from rich.console import Console
from rich.markdown import Markdown


def render_markdown(text: str, *, width: int = 80) -> str:
    """Return an ANSI-coded string for the given markdown text."""
    return _render(Markdown(text), width=width)


def render_plain(text: str, *, width: int = 80) -> str:
    """Return an ANSI-coded string for plain text (no markdown parsing)."""
    return _render(text, width=width)


def _render(renderable, *, width: int) -> str:
    buffer = StringIO()
    console = Console(
        file=buffer,
        width=max(20, width),
        color_system="standard",
        force_terminal=True,
        highlight=False,
        soft_wrap=False,
    )
    console.print(renderable, end="")
    return buffer.getvalue()
