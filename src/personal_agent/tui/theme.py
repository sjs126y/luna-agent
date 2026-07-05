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
USER_BAR = "36"       # user prompt bar ▍: cyan
USER = "1"            # user name: bold
AGENT_BAR = "35"      # agent prompt bar ▍: magenta
AGENT = "1;35"        # "Agent" label: bold magenta
TOOL_ACTIVE = "36"    # running tool line: cyan
TOOL_OK = "32"        # completed tool ✓: green
TOOL_ERR = "31"       # failed tool ✗: red
THINKING = "2"        # thinking hint: dim
EXPAND_HEADER = "34"  # Ctrl+O expand header: blue
STATUS = "2"          # status bar: dim
STATUS_ACCENT = "36"  # status bar highlighted segment (mode): cyan
CONFIRM = "1;33"      # inline tool confirmation prompt: bold yellow

# Vertical bar that leads user / agent lines (CC/Codex-style gutter).
BAR = "▍"

# Sparkline ramp for the tiny context-usage meter in the status bar.
_SPARK = "▁▂▃▄▅▆▇█"


def sgr(text: str, code: str) -> str:
    """Wrap ``text`` in the given SGR code and reset."""
    if not code:
        return text
    return f"\x1b[{code}m{text}{RESET}"


def dim(text: str) -> str:
    return sgr(text, DIM)


def divider(width: int) -> str:
    """A dim horizontal rule that fills the given terminal width."""
    return dim("─" * max(1, width))


def gutter(bar_code: str, label: str, label_code: str) -> str:
    """Render a leading colored bar + label, e.g. ``▍你`` / ``▍Agent``."""
    return f"{sgr(BAR, bar_code)}{sgr(label, label_code)}"


def spark_meter(fraction: float, cells: int = 5) -> str:
    """A tiny sparkline bar for a 0..1 fraction (context usage)."""
    fraction = max(0.0, min(1.0, fraction))
    if cells <= 0:
        return ""
    # distribute the fraction across cells; each cell picks a ramp glyph
    per = fraction * cells
    out: list[str] = []
    for i in range(cells):
        level = max(0.0, min(1.0, per - i))
        idx = int(round(level * (len(_SPARK) - 1)))
        out.append(_SPARK[idx])
    return "".join(out)
