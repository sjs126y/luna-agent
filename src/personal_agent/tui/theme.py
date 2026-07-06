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
# the palette can be retuned without renaming call sites. Values stay within the
# terminal's own 16-color palette so the UI follows the user's theme (CC/Codex
# minimal look) instead of fighting it.
DIM = "2"
BOLD = "1"
PROMPT = "1;36"       # input prompt symbol ❯: bold cyan
USER_BAR = "34"       # user message left bar ▌: blue (distinct from AI)
USER_MSG = "39"       # user message text: default fg (full contrast, readable)
METER_MODEL = "36"    # model name above the prompt: readable, not ghosted
HINT_LABEL = "37"     # shortcut labels: quieter than keys, still legible
AGENT_BAR = "35"      # agent streaming bar ▍: magenta
AGENT = "1;35"        # "Agent" label: bold magenta
TOOL_ACTIVE = "36"    # running tool line: cyan
TOOL_OK = "32"        # completed tool ✓: green
TOOL_ERR = "31"       # failed tool ✗: red
TOOL_HINT = "2"       # "(Ctrl+O 展开)" hint: dim
THINKING = "2"        # thinking hint: dim
NOTICE = "33"         # retry/stop/compression notices: yellow
ERROR = "1;31"        # turn-level errors: bold red
EXPAND_HEADER = "34"  # Ctrl+O expand header: blue
KEY = "36"            # keyboard-hint keys (⏎, Ctrl+J…): cyan
CONFIRM = "1;33"      # inline tool confirmation prompt: bold yellow

# Per-mode accent colors so the current execution mode is instantly readable.
MODE_STYLES = {
    "Read Only": "32",
    "Ask First": "36",
    "Edit Freely": "33",
    "Full Auto": "1;31",
    # Legacy names are kept for old cached/runtime values.
    "normal": "36",
    "acceptEdits": "33",
    "auto": "1;31",
}


def mode_style(mode: str) -> str:
    return MODE_STYLES.get(mode, "36")


def meter_style(fraction: float) -> str:
    """Color the context meter by how full it is: green→yellow→red."""
    if fraction >= 0.8:
        return "31"   # red
    if fraction >= 0.5:
        return "33"   # yellow
    return "32"       # green

# Vertical bars that lead message / streaming lines.
BAR = "▍"
USER_BARCH = "▌"


def sgr(text: str, code: str) -> str:
    """Wrap ``text`` in the given SGR code and reset."""
    if not code:
        return text
    return f"\x1b[{code}m{text}{RESET}"


def dim(text: str) -> str:
    return sgr(text, DIM)


def humanize(n: int) -> str:
    """Compact token count: 1659 -> 1.7k, 1_000_000 -> 1M."""
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        v = n / 1000
        return (f"{v:.1f}".rstrip("0").rstrip(".")) + "k"
    v = n / 1_000_000
    return (f"{v:.1f}".rstrip("0").rstrip(".")) + "M"


def bar_meter(fraction: float, cells: int = 10) -> str:
    """A solid progress bar for a 0..1 fraction, e.g. ▰▰▰▱▱▱▱▱▱▱."""
    fraction = max(0.0, min(1.0, fraction))
    if cells <= 0:
        return ""
    filled = int(round(fraction * cells))
    return "▰" * filled + "▱" * (cells - filled)
