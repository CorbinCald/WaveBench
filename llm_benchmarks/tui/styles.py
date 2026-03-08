import os
import re
import sys
import shutil

_NO_COLOR = not sys.stdout.isatty() or os.environ.get("NO_COLOR") is not None

class S:
    """ANSI escape codes — empty strings when color is disabled."""
    RST  = "" if _NO_COLOR else "\033[0m"
    BOLD = "" if _NO_COLOR else "\033[1m"
    DIM  = "" if _NO_COLOR else "\033[2m"
    RED  = "" if _NO_COLOR else "\033[31m"
    GRN  = "" if _NO_COLOR else "\033[32m"
    YEL  = "" if _NO_COLOR else "\033[33m"
    BLU  = "" if _NO_COLOR else "\033[34m"
    CYN  = "" if _NO_COLOR else "\033[36m"
    HRED = "" if _NO_COLOR else "\033[91m"
    HGRN = "" if _NO_COLOR else "\033[92m"
    HYEL = "" if _NO_COLOR else "\033[93m"
    HBLU = "" if _NO_COLOR else "\033[94m"
    HCYN = "" if _NO_COLOR else "\033[96m"
    HWHT = "" if _NO_COLOR else "\033[97m"

PHASE_GRADIENT: list[str] = [
    "" if _NO_COLOR else "\033[38;2;30;65;187m",     # solid deep blue
    "" if _NO_COLOR else "\033[38;2;40;105;204m",    # medium blue
    "" if _NO_COLOR else "\033[38;2;55;150;221m",    # teal-blue
    "" if _NO_COLOR else "\033[38;2;75;200;238m",    # muted cyan
    "" if _NO_COLOR else "\033[38;2;102;255;255m",   # bright neon cyan
]

PULSE_GRADIENT: list[str] = [
    "" if _NO_COLOR else "\033[38;2;0;28;8m",
    "" if _NO_COLOR else "\033[38;2;0;48;14m",
    "" if _NO_COLOR else "\033[38;2;0;72;20m",
    "" if _NO_COLOR else "\033[38;2;0;100;28m",
    "" if _NO_COLOR else "\033[38;2;0;135;38m",
    "" if _NO_COLOR else "\033[38;2;0;172;48m",
    "" if _NO_COLOR else "\033[38;2;0;210;58m",
    "" if _NO_COLOR else "\033[38;2;35;240;82m",
    "" if _NO_COLOR else "\033[38;2;100;255;140m",
]

PULSE_DIM: str = "" if _NO_COLOR else "\033[38;2;0;30;8m"

_ok    = f"{S.HGRN}✓{S.RST}"
_fail  = f"{S.HRED}✗{S.RST}"
_wait  = f"{S.HYEL}●{S.RST}"
_work  = f"{S.HCYN}◌{S.RST}"
_skip  = f"{S.DIM}○{S.RST}"
_arrow = f"{S.DIM}→{S.RST}"
_dot   = f"{S.DIM}·{S.RST}"
_tri   = f"{S.DIM}▸{S.RST}"

_SPIN  = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

def _vlen(text: str) -> int:
    """Visible length of *text*, ignoring ANSI escape sequences."""
    return len(re.sub(r'\033\[[0-9;]*m', '', str(text)))

def _rpad(text: str, width: int) -> str:
    """Left-align *text* to *width*, ANSI-aware."""
    return text + " " * max(0, width - _vlen(text))

def _tw() -> int:
    """Terminal width, clamped to [20, 120]."""
    return max(20, min(120, shutil.get_terminal_size((80, 24)).columns))

def _rule(label: str = "", heavy: bool = False) -> None:
    """Print a horizontal rule with an optional section label."""
    w = _tw() - 4
    ch = "━" if heavy else "─"
    if label:
        vis = _vlen(label)
        seg = (ch * 2
               + f" {S.BOLD}{S.CYN}{label}{S.RST}{S.DIM} "
               + ch * max(1, w - vis - 4))
    else:
        seg = ch * w
    print(f"  {S.DIM}{seg}{S.RST}")

def _box_top(title: str = "", width: int = 0, heavy: bool = False) -> str:
    """Return the top border of a btop-style box with optional embedded title."""
    if not width:
        width = _tw() - 4
    ch, lc, rc = ("━", "┏", "┓") if heavy else ("─", "╭", "╮")
    if title:
        vis = _vlen(title)
        fill = max(1, width - 5 - vis)
        return (f"  {S.DIM}{lc}{ch} {S.RST}{S.BOLD}{S.CYN}{title}{S.RST}"
                f"{S.DIM} {ch * fill}{rc}{S.RST}")
    return f"  {S.DIM}{lc}{ch * (width - 2)}{rc}{S.RST}"

def _box_row(content: str = "", width: int = 0, heavy: bool = False) -> str:
    """Return a box row with content padded to fill the inner width."""
    if not width:
        width = _tw() - 4
    sc = "┃" if heavy else "│"
    inner = width - 4
    pad = max(0, inner - _vlen(content))
    return f"  {S.DIM}{sc}{S.RST} {content}{' ' * pad} {S.DIM}{sc}{S.RST}"

def _box_sep(label: str = "", width: int = 0) -> str:
    """Return a box separator with optional embedded label."""
    if not width:
        width = _tw() - 4
    if label:
        vis = _vlen(label)
        fill = max(1, width - 5 - vis)
        return (f"  {S.DIM}├─ {S.RST}{S.BOLD}{S.CYN}{label}{S.RST}"
                f"{S.DIM} {'─' * fill}┤{S.RST}")
    return f"  {S.DIM}├{'─' * (width - 2)}┤{S.RST}"

def _box_bot(width: int = 0, heavy: bool = False) -> str:
    """Return the bottom border of a box."""
    if not width:
        width = _tw() - 4
    ch, lc, rc = ("━", "┗", "┛") if heavy else ("─", "╰", "╯")
    return f"  {S.DIM}{lc}{ch * (width - 2)}{rc}{S.RST}"

def _box(title: str, lines: list, width: int = 0, heavy: bool = False) -> None:
    """Print content inside a bordered box."""
    if not width:
        width = _tw() - 4
    print(_box_top(title, width, heavy))
    for line in lines:
        print(_box_row(line, width, heavy))
    print(_box_bot(width, heavy))

def format_duration(seconds: float | None) -> str:
    """Format *seconds* into a concise human-readable string."""
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(seconds, 60)
    return f"{int(m)}m {s:.0f}s"

def _truncate(text: str, length: int = 60) -> str:
    """Truncate *text* with an ellipsis if needed."""
    return text if len(text) <= length else text[:length - 1] + "…"

def format_cost(dollars: float | None) -> str:
    """Format a dollar amount into a compact cost string."""
    if dollars is None or dollars <= 0:
        return ""
    if dollars < 0.001:
        return f"${dollars:.4f}"
    if dollars < 0.10:
        return f"${dollars:.3f}"
    if dollars < 10:
        return f"${dollars:.2f}"
    return f"${dollars:,.2f}"
