"""Central color/style palette for the inline TUI.

Every ANSI escape the inline TUI emits should come from here, so recoloring the
UI means editing one file (not hunting raw ``\\x1b[...`` codes across modules).
Helpers wrap text in SGR codes and always reset, so callers never leak styling
into following output.

These are SGR sequences (not prompt_toolkit styles) because the inline renderer
prints finalized content straight to the terminal via ``run_in_terminal`` and
feeds the active region through ``ANSI(...)``.
"""

from __future__ import annotations

RESET = "\x1b[0m"

# Semantic SGR codes. Keep names intent-based (role) rather than color-based, so
# the palette can be retuned without renaming call sites.
DIM = "2"
BOLD = "1"
USER = "1"            # user echo line: bold
AGENT = "1;36"        # "Personal Agent:" label: bold cyan
TOOL_ACTIVE = "36"    # running tool line: cyan
TOOL_OK = "32"        # completed tool ✓: green
TOOL_ERR = "31"       # failed tool ✗: red
THINKING = "2"        # thinking hint: dim
EXPAND_HEADER = "34"  # Ctrl+O expand header: blue
STATUS = "2"          # status bar: dim
CONFIRM = "1;33"      # inline tool confirmation prompt: bold yellow


def sgr(text: str, code: str) -> str:
    """Wrap ``text`` in the given SGR code and reset."""
    if not code:
        return text
    return f"\x1b[{code}m{text}{RESET}"


def dim(text: str) -> str:
    return sgr(text, DIM)
