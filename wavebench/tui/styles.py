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

# Indices 7-8 must stay saturated (low off-channels) — high RGB across all
# channels washes out to white on full braille blocks.
# Floor lightness is ~12% so the darkest level stays visible on dark terminals.
PULSE_GRADIENT: list[str] = [
    "" if _NO_COLOR else "\033[38;2;0;61;17m",
    "" if _NO_COLOR else "\033[38;2;0;82;23m",
    "" if _NO_COLOR else "\033[38;2;0;102;29m",
    "" if _NO_COLOR else "\033[38;2;0;128;36m",
    "" if _NO_COLOR else "\033[38;2;0;153;43m",
    "" if _NO_COLOR else "\033[38;2;0;178;51m",
    "" if _NO_COLOR else "\033[38;2;0;204;58m",
    "" if _NO_COLOR else "\033[38;2;0;230;65m",
    "" if _NO_COLOR else "\033[38;2;0;250;71m",
]

TITLE_WAVE_GRADIENT: list[str] = [
    "",                                                     # 0: unused
    "" if _NO_COLOR else "\033[38;2;0;51;14m",              # 1: darkest
    "" if _NO_COLOR else "\033[38;2;0;71;20m",              # 2
    "" if _NO_COLOR else "\033[38;2;0;92;26m",              # 3
    "" if _NO_COLOR else "\033[38;2;0;117;33m",             # 4
    "" if _NO_COLOR else "\033[38;2;0;143;40m",             # 5
    "" if _NO_COLOR else "\033[38;2;0;173;49m",             # 6
    "" if _NO_COLOR else "\033[38;2;0;204;58m",             # 7
    "" if _NO_COLOR else "\033[38;2;0;230;65m",             # 8: peak saturated
]

PULSE_DIM: str = "" if _NO_COLOR else "\033[38;2;0;51;14m"


def _rgb(r: int, g: int, b: int) -> str:
    return "" if _NO_COLOR else f"\033[38;2;{r};{g};{b}m"


THEMES: dict[str, dict] = {
    "default": {
        "phase": [
            (13, 115, 42), (16, 147, 53), (20, 179, 65),
            (23, 211, 77), (39, 231, 94),
        ],
        "pulse": [
            (0, 61, 17), (0, 82, 23), (0, 102, 29), (0, 128, 36),
            (0, 153, 43), (0, 178, 51), (0, 204, 58),
            (0, 230, 65), (0, 250, 71),
        ],
        "title_wave": [
            None, (0, 51, 14), (0, 71, 20), (0, 92, 26),
            (0, 117, 33), (0, 143, 40), (0, 173, 49),
            (0, 204, 58), (0, 230, 65),
        ],
        "pulse_dim": (0, 51, 14),
        "idle_wave": ((0, 71, 20), (0, 163, 46), (0, 255, 72)),
        "pre_wave": {
            "start_base": (0, 230, 65), "start_amp": (15, 15, 15),
            "target_base": (25, 102, 47), "target_amp": (12, 12, 12),
        },
        "accent": "\033[38;2;0;204;58m",
        "accent_hi": "\033[38;2;0;250;71m",
        "border": (30, 62, 39),
    },
    "plum": {
        "phase": [
            (96, 0, 128), (122, 0, 163), (149, 0, 199),
            (176, 0, 235), (195, 15, 255),
        ],
        "pulse": [
            (46, 0, 61), (61, 0, 82), (77, 0, 102), (96, 0, 128),
            (115, 0, 153), (134, 0, 178), (153, 0, 204),
            (172, 0, 230), (187, 0, 250),
        ],
        "title_wave": [
            None, (38, 0, 51), (54, 0, 71), (69, 0, 92),
            (88, 0, 117), (107, 0, 143), (130, 0, 173),
            (153, 0, 204), (172, 0, 230),
        ],
        "pulse_dim": (38, 0, 51),
        "idle_wave": ((54, 0, 71), (122, 0, 163), (191, 0, 255)),
        "pre_wave": {
            "start_base": (172, 0, 230), "start_amp": (15, 15, 15),
            "target_base": (83, 25, 102), "target_amp": (12, 12, 12),
        },
        "accent": "\033[38;2;153;0;204m",
        "accent_hi": "\033[38;2;187;0;250m",
        "border": (54, 30, 62),
    },
    "lemon": {
        "phase": [
            (118, 109, 10), (151, 139, 12), (184, 170, 15),
            (217, 200, 18), (237, 220, 33),
        ],
        "pulse": [
            (61, 56, 0), (82, 75, 0), (102, 94, 0), (128, 117, 0),
            (153, 140, 0), (178, 164, 0), (204, 187, 0),
            (230, 210, 0), (250, 229, 0),
        ],
        "title_wave": [
            None, (51, 47, 0), (71, 65, 0), (92, 84, 0),
            (117, 108, 0), (143, 131, 0), (173, 159, 0),
            (204, 187, 0), (230, 210, 0),
        ],
        "pulse_dim": (51, 47, 0),
        "idle_wave": ((71, 65, 0), (163, 150, 0), (255, 234, 0)),
        "pre_wave": {
            "start_base": (230, 210, 0), "start_amp": (15, 15, 15),
            "target_base": (102, 96, 25), "target_amp": (12, 12, 12),
        },
        "accent": "\033[38;2;204;187;0m",
        "accent_hi": "\033[38;2;250;229;0m",
        "border": (62, 59, 30),
    },
    "blueberry": {
        "phase": [
            (10, 64, 118), (12, 82, 151), (15, 99, 184),
            (18, 117, 217), (33, 135, 237),
        ],
        "pulse": [
            (0, 31, 61), (0, 41, 82), (0, 51, 102), (0, 64, 128),
            (0, 76, 153), (0, 89, 178), (0, 102, 204),
            (0, 115, 230), (0, 125, 250),
        ],
        "title_wave": [
            None, (0, 25, 51), (0, 36, 71), (0, 46, 92),
            (0, 59, 117), (0, 71, 143), (0, 87, 173),
            (0, 102, 204), (0, 115, 230),
        ],
        "pulse_dim": (0, 25, 51),
        "idle_wave": ((0, 36, 71), (0, 82, 163), (0, 127, 255)),
        "pre_wave": {
            "start_base": (0, 115, 230), "start_amp": (15, 15, 15),
            "target_base": (25, 64, 102), "target_amp": (12, 12, 12),
        },
        "accent": "\033[38;2;0;102;204m",
        "accent_hi": "\033[38;2;0;125;250m",
        "border": (30, 46, 62),
    },
    "grape": {
        "phase": [
            (71, 19, 108), (91, 24, 139), (111, 30, 169),
            (131, 35, 199), (149, 51, 219),
        ],
        "pulse": [
            (35, 6, 55), (46, 8, 73), (58, 10, 92), (72, 13, 115),
            (87, 15, 138), (101, 18, 161), (116, 20, 184),
            (130, 23, 207), (142, 25, 225),
        ],
        "title_wave": [
            None, (29, 5, 46), (40, 7, 64), (52, 9, 83),
            (66, 12, 106), (81, 14, 129), (98, 17, 156),
            (116, 20, 184), (130, 23, 207),
        ],
        "pulse_dim": (29, 5, 46),
        "idle_wave": ((40, 7, 64), (92, 16, 147), (144, 25, 230)),
        "pre_wave": {
            "start_base": (130, 23, 207), "start_amp": (15, 15, 15),
            "target_base": (69, 33, 94), "target_amp": (12, 12, 12),
        },
        "accent": "\033[38;2;116;20;184m",
        "accent_hi": "\033[38;2;142;25;225m",
        "border": (48, 33, 59),
    },
    "pear": {
        "phase": [
            (41, 86, 47), (53, 110, 61), (65, 134, 74),
            (76, 158, 87), (93, 177, 104),
        ],
        "pulse": [
            (20, 41, 23), (27, 55, 30), (33, 69, 38), (41, 86, 47),
            (50, 103, 57), (58, 120, 66), (66, 138, 76),
            (75, 155, 85), (81, 169, 93),
        ],
        "title_wave": [
            None, (17, 34, 19), (23, 48, 27), (30, 62, 34),
            (38, 79, 44), (46, 96, 53), (56, 117, 64),
            (66, 138, 76), (75, 155, 85),
        ],
        "pulse_dim": (17, 34, 19),
        "idle_wave": ((23, 48, 27), (53, 110, 61), (83, 172, 95)),
        "pre_wave": {
            "start_base": (75, 155, 85), "start_amp": (15, 15, 15),
            "target_base": (50, 77, 54), "target_amp": (12, 12, 12),
        },
        "accent": "\033[38;2;66;138;76m",
        "accent_hi": "\033[38;2;81;169;93m",
        "border": (39, 53, 41),
    },
    "acai": {
        "phase": [
            (10, 69, 118), (12, 89, 151), (15, 108, 184),
            (18, 127, 217), (33, 145, 237),
        ],
        "pulse": [
            (4, 33, 58), (5, 44, 77), (6, 55, 96), (8, 69, 120),
            (9, 83, 144), (11, 97, 168), (12, 111, 192),
            (14, 125, 216), (15, 136, 235),
        ],
        "title_wave": [
            None, (3, 28, 48), (4, 39, 67), (6, 50, 86),
            (7, 64, 110), (9, 78, 134), (10, 94, 163),
            (12, 111, 192), (14, 125, 216),
        ],
        "pulse_dim": (3, 28, 48),
        "idle_wave": ((4, 39, 67), (10, 89, 153), (15, 139, 240)),
        "pre_wave": {
            "start_base": (14, 125, 216), "start_amp": (15, 15, 15),
            "target_base": (31, 67, 97), "target_amp": (12, 12, 12),
        },
        "accent": "\033[38;2;12;111;192m",
        "accent_hi": "\033[38;2;15;136;235m",
        "border": (32, 47, 60),
    },
    "tangerine": {
        "phase": [
            (121, 31, 6), (155, 40, 8), (189, 49, 10),
            (223, 57, 12), (243, 74, 27),
        ],
        "pulse": [
            (59, 15, 2), (78, 20, 3), (98, 24, 4), (122, 31, 5),
            (147, 37, 6), (171, 43, 7), (196, 49, 8),
            (220, 55, 9), (240, 60, 10),
        ],
        "title_wave": [
            None, (49, 12, 2), (69, 17, 3), (88, 22, 4),
            (113, 28, 5), (137, 34, 6), (166, 42, 7),
            (196, 49, 8), (220, 55, 9),
        ],
        "pulse_dim": (49, 12, 2),
        "idle_wave": ((69, 17, 3), (157, 39, 7), (245, 61, 10)),
        "pre_wave": {
            "start_base": (220, 55, 9), "start_amp": (15, 15, 15),
            "target_base": (99, 44, 29), "target_amp": (12, 12, 12),
        },
        "accent": "\033[38;2;196;49;8m",
        "accent_hi": "\033[38;2;240;60;10m",
        "border": (61, 38, 31),
    },
    "lime": {
        "phase": [
            (64, 118, 10), (82, 151, 12), (99, 184, 15),
            (117, 217, 18), (135, 237, 33),
        ],
        "pulse": [
            (31, 61, 0), (41, 82, 0), (51, 102, 0), (64, 128, 0),
            (77, 153, 0), (89, 178, 0), (102, 204, 0),
            (115, 230, 0), (125, 250, 0),
        ],
        "title_wave": [
            None, (26, 51, 0), (36, 71, 0), (46, 92, 0),
            (59, 117, 0), (71, 143, 0), (87, 173, 0),
            (102, 204, 0), (115, 230, 0),
        ],
        "pulse_dim": (26, 51, 0),
        "idle_wave": ((36, 71, 0), (82, 163, 0), (128, 255, 0)),
        "pre_wave": {
            "start_base": (115, 230, 0), "start_amp": (15, 15, 15),
            "target_base": (64, 102, 25), "target_amp": (12, 12, 12),
        },
        "accent": "\033[38;2;102;204;0m",
        "accent_hi": "\033[38;2;125;250;0m",
        "border": (46, 62, 30),
    },
}

THEME_NAMES: list[str] = list(THEMES.keys())

IDLE_WAVE_COLORS: tuple = THEMES["default"]["idle_wave"]
PRE_WAVE_COLORS: dict = dict(THEMES["default"]["pre_wave"])
ACCENT: str = "" if _NO_COLOR else "\033[38;2;0;204;58m"
ACCENT_HI: str = "" if _NO_COLOR else "\033[38;2;0;250;71m"
BORDER: str = "" if _NO_COLOR else "\033[38;2;30;62;39m"


def apply_theme(name: str) -> None:
    """Apply a named color theme, updating all module-level gradient variables."""
    global PHASE_GRADIENT, PULSE_GRADIENT, TITLE_WAVE_GRADIENT, PULSE_DIM
    global IDLE_WAVE_COLORS, PRE_WAVE_COLORS, ACCENT, ACCENT_HI, BORDER

    theme = THEMES.get(name, THEMES["default"])

    PHASE_GRADIENT[:] = [_rgb(*c) for c in theme["phase"]]
    PULSE_GRADIENT[:] = [_rgb(*c) for c in theme["pulse"]]
    TITLE_WAVE_GRADIENT[:] = [
        "" if c is None else _rgb(*c) for c in theme["title_wave"]
    ]
    PULSE_DIM = _rgb(*theme["pulse_dim"])
    IDLE_WAVE_COLORS = theme["idle_wave"]
    PRE_WAVE_COLORS = dict(theme["pre_wave"])
    ACCENT = "" if _NO_COLOR else theme["accent"]
    ACCENT_HI = "" if _NO_COLOR else theme["accent_hi"]
    BORDER = _rgb(*theme["border"])


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
               + f" {S.RST}{S.BOLD}{ACCENT}{label}{S.RST}{BORDER} "
               + ch * max(1, w - vis - 4))
    else:
        seg = ch * w
    print(f"  {BORDER}{seg}{S.RST}")

def _box_top(title: str = "", width: int = 0, heavy: bool = False) -> str:
    """Return the top border of a btop-style box with optional embedded title."""
    if not width:
        width = _tw() - 4
    ch, lc, rc = ("━", "┏", "┓") if heavy else ("─", "╭", "╮")
    if title:
        vis = _vlen(title)
        fill = max(1, width - 5 - vis)
        return (f"  {BORDER}{lc}{ch} {S.RST}{S.BOLD}{ACCENT}{title}{S.RST}"
                f"{BORDER} {ch * fill}{rc}{S.RST}")
    return f"  {BORDER}{lc}{ch * (width - 2)}{rc}{S.RST}"

def _box_row(content: str = "", width: int = 0, heavy: bool = False) -> str:
    """Return a box row with content padded to fill the inner width."""
    if not width:
        width = _tw() - 4
    sc = "┃" if heavy else "│"
    inner = width - 4
    pad = max(0, inner - _vlen(content))
    return f"  {BORDER}{sc}{S.RST} {content}{' ' * pad} {BORDER}{sc}{S.RST}"

def _box_sep(label: str = "", width: int = 0) -> str:
    """Return a box separator with optional embedded label."""
    if not width:
        width = _tw() - 4
    if label:
        vis = _vlen(label)
        fill = max(1, width - 5 - vis)
        return (f"  {BORDER}├─ {S.RST}{S.BOLD}{ACCENT}{label}{S.RST}"
                f"{BORDER} {'─' * fill}┤{S.RST}")
    return f"  {BORDER}├{'─' * (width - 2)}┤{S.RST}"

def _box_bot(width: int = 0, heavy: bool = False) -> str:
    """Return the bottom border of a box."""
    if not width:
        width = _tw() - 4
    ch, lc, rc = ("━", "┗", "┛") if heavy else ("─", "╰", "╯")
    return f"  {BORDER}{lc}{ch * (width - 2)}{rc}{S.RST}"

def _box(title: str, lines: list, width: int = 0, heavy: bool = False) -> None:
    """Print content inside a bordered box."""
    if not width:
        width = _tw() - 4
    print(_box_top(title, width, heavy))
    for line in lines:
        print(_box_row(line, width, heavy))
    print(_box_bot(width, heavy))

def _banner(title: str, width: int = 0) -> str:
    """Render a centered title in a heavy box — used for the main app header."""
    if not width:
        width = _tw() - 4
    inner = width - 4
    tlen = _vlen(title)
    lp = (inner - tlen) // 2
    rp = inner - tlen - lp
    return '\n'.join([
        f"  {BORDER}┏{'━' * (width - 2)}┓{S.RST}",
        f"  {BORDER}┃{S.RST} {' ' * lp}{S.BOLD}{ACCENT}{title}{S.RST}"
        f"{' ' * rp} {BORDER}┃{S.RST}",
        f"  {BORDER}┗{'━' * (width - 2)}┛{S.RST}",
    ])


def _box_divider(width: int = 0, heavy: bool = False) -> str:
    """A light dotted divider inside a box (does not connect to the walls)."""
    if not width:
        width = _tw() - 4
    sc = "┃" if heavy else "│"
    inner = width - 4
    return f"  {BORDER}{sc}  {'┄' * (inner - 2)}  {sc}{S.RST}"


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
