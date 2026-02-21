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
    """Terminal width, clamped to [60, 120]."""
    return max(60, min(120, shutil.get_terminal_size((80, 24)).columns))

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
