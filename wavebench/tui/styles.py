"""Theme system, color primitives, and ANSI-aware string utilities.

Defines the nine built-in themes (``default`` + 8 fruit-named variants),
hue-locked lightness gradients for the wave animation, per-theme RGB
accent colors, box-drawing characters, duration and number formatters,
and width-measuring helpers that strip ANSI codes before counting.

Theme state is process-global: changing the theme updates module-level
color constants that the rest of the TUI imports directly. The live
theme preview in the config menu swaps colors and reverts on cancel.
"""

import functools
import os
import re
import shutil
import subprocess
import sys

_NO_COLOR = not sys.stdout.isatty() or os.environ.get("NO_COLOR") is not None


def _detect_truecolor() -> bool:
    """Whether 24-bit color actually reaches the screen.

    ``COLORTERM`` is inherited by child processes, so inside a multiplexer it
    describes the terminal tmux is drawing *to*, not what tmux forwards: unless
    RGB passthrough is configured, tmux re-quantises every 24-bit sequence down
    to its 256-color palette. Trusting the variable there makes the app emit
    gradients the screen cannot show, so ask tmux what it really forwards.
    """
    override = os.environ.get("WAVEBENCH_COLOR_DEPTH", "").strip().lower()
    if override:
        return override in ("truecolor", "24bit")

    if os.environ.get("TMUX"):
        try:
            features = subprocess.run(
                ["tmux", "display-message", "-p", "#{client_termfeatures}"],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return "RGB" in features.strip().split(",")

    if os.environ.get("TERM", "").startswith("screen"):
        return False
    return os.environ.get("COLORTERM", "").strip().lower() in ("truecolor", "24bit")


TRUECOLOR: bool = _detect_truecolor()

# Channel levels of the xterm-256 color cube, and the luminance weight of each
# channel. The weights double as a perceptual yardstick for hue comparison: an
# error in blue, which the eye barely resolves, costs a fraction of one in green.
_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)
_LUMA_WEIGHTS = (0.2126, 0.7152, 0.0722)
_RAMP_SAMPLES = 16


def _luma(color: tuple[int, int, int]) -> float:
    return sum(weight * value for weight, value in zip(_LUMA_WEIGHTS, color, strict=True))


def _hue_of(color: tuple[int, int, int]) -> tuple[float, ...]:
    peak = max(color)
    return (0.0, 0.0, 0.0) if peak == 0 else tuple(value / peak for value in color)


def _hue_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return sum(
        weight * abs(x - y)
        for weight, x, y in zip(_LUMA_WEIGHTS, _hue_of(a), _hue_of(b), strict=True)
    )


_CUBE_TOPS = tuple(
    (r, g, b)
    for r in _CUBE_LEVELS
    for g in _CUBE_LEVELS
    for b in _CUBE_LEVELS
    if max(r, g, b) == 255 and not r == g == b
)


@functools.lru_cache(maxsize=64)
def hue_ramp(color: tuple[int, int, int]) -> tuple[tuple[int, int, int], ...]:
    """Dark-to-bright 256-palette ramp holding *color*'s hue.

    The color cube's lowest non-zero level is 0x5f, so it has no dark saturated
    entries at all — while the 24-entry greyscale ramp sits densely right beside
    them. A terminal picking the nearest palette color therefore renders dim
    theme colors as *grey*, and one step of lightness can flip a cell from
    colored to colorless. So the ramp is built rather than searched: scale the
    channel *indices* of the best-matching bright entry, then keep only the
    steps whose channel ratios fade monotonically toward the dominant channel as
    they darken. That second rule is what stops the ramp doubling back on itself
    — plain index scaling walks lime through pure green, *yellow*, then back to
    yellow-green — and it drops achromatic steps for free, since reaching the
    grey diagonal means a secondary ratio rose to 1.0.

    It is deliberately strict, and it is why a ramp is often only three steps
    long. Relaxing it does not buy a smoother gradient: below 0x5f the cube has
    no medium-saturation entries at all, so the only darker neighbours are fully
    saturated ones. Pear, for instance, can reach (0, 95, 0) or (95, 135, 95) —
    ratios (0, 1, 0) against (0.7, 1, 0.7). Admitting both would put a visible
    color change partway down the water, which is the artifact this whole
    module exists to avoid. Black is excluded too: with nothing to dither
    against it would swallow whole rows of the deepest water.
    """
    brightest = min(_CUBE_TOPS, key=lambda entry: _hue_distance(entry, color))
    indices = [_CUBE_LEVELS.index(value) for value in brightest]

    steps: list[tuple[int, int, int]] = []
    for sample in range(_RAMP_SAMPLES):
        scale = sample / (_RAMP_SAMPLES - 1)
        entry = tuple(_CUBE_LEVELS[round(index * scale)] for index in indices)
        if max(entry) and entry not in steps:
            steps.append(entry)

    kept: list[tuple[int, int, int]] = []
    ceiling = (1.0, 1.0, 1.0)
    for entry in reversed(steps):  # brightest first
        ratios = _hue_of(entry)
        if any(ratio > limit + 1e-9 for ratio, limit in zip(ratios, ceiling, strict=True)):
            continue
        kept.append(entry)
        ceiling = ratios
    return tuple(reversed(kept))


def _palette_escape(entry: tuple[int, int, int]) -> str:
    r, g, b = (_CUBE_LEVELS.index(value) for value in entry)
    return f"\033[38;5;{16 + 36 * r + 6 * g + b}m"


@functools.lru_cache(maxsize=4096)
def palette_code(color: tuple[int, int, int]) -> str:
    """Nearest on-hue 256-palette escape — never grey, never black.

    Three to five steps is all the cube affords a saturated hue, so the depth
    gradient lands as bands rather than a fade. Dithering between the steps
    would smooth it, but only by alternating color every few cells — and in a
    terminal a run of one color is nearly free while an alternating one costs an
    escape sequence per cell. Measured on a 300-column ocean that was 3030
    escapes a frame against 158, enough for the render to outrun the terminal
    and starve the key-reading loop it shares a thread with. Bands that hold
    still beat a fade you cannot type through.
    """
    target = _luma(color)
    return _palette_escape(min(hue_ramp(color), key=lambda s: abs(_luma(s) - target)))


def color_code(color: tuple[int, int, int]) -> str:
    """Escape for *color*, matched to what the terminal can actually render.

    Does not consult ``NO_COLOR`` — callers gate on that themselves.
    """
    if TRUECOLOR:
        return f"\033[38;2;{color[0]};{color[1]};{color[2]}m"
    return palette_code(color)


class S:
    """ANSI escape codes — empty strings when color is disabled."""

    RST = "" if _NO_COLOR else "\033[0m"
    BOLD = "" if _NO_COLOR else "\033[1m"
    DIM = "" if _NO_COLOR else "\033[2m"
    RED = "" if _NO_COLOR else "\033[31m"
    GRN = "" if _NO_COLOR else "\033[32m"
    YEL = "" if _NO_COLOR else "\033[33m"
    BLU = "" if _NO_COLOR else "\033[34m"
    CYN = "" if _NO_COLOR else "\033[36m"
    HRED = "" if _NO_COLOR else "\033[91m"
    HGRN = "" if _NO_COLOR else "\033[92m"
    HYEL = "" if _NO_COLOR else "\033[93m"
    HBLU = "" if _NO_COLOR else "\033[94m"
    HCYN = "" if _NO_COLOR else "\033[96m"
    HWHT = "" if _NO_COLOR else "\033[97m"


# Display control, deliberately *not* gated on NO_COLOR: these move and hide the
# cursor rather than colour anything, and a monochrome terminal needs them just
# as much as a colour one.
CURSOR_HIDE = "\033[?25l"
CURSOR_SHOW = "\033[?25h"
CURSOR_SAVE = "\0337"  # DECSC — position + attributes, not visibility
CURSOR_RESTORE = "\0338"  # DECRC
SYNC_BEGIN = "\033[?2026h"  # synchronized output; ignored where unsupported
SYNC_END = "\033[?2026l"


def overlay_frame(body: str) -> str:
    """Wrap a full-screen repaint that must not disturb the cursor.

    A wave frame runs to tens of kilobytes, but a PTY accepts only ~4 KB per
    write, so the terminal receives one frame as several chunks and repaints
    between them. Left unguarded it draws the block cursor over whichever cell
    the stream happened to reach — a white-on-black artifact at a different
    random position every frame.

    Two layers stop that. ``?2026`` asks terminals that support synchronized
    output to present the whole frame at once, so no intermediate state is ever
    shown. Terminals without it still never see a stray cursor, because the
    frame hides it up front and only shows it again after DECRC has put it back
    where the caller left it. The hide/show pair sits *inside* the synchronized
    block so terminals honouring ``?2026`` never render the hidden state at all.
    """
    return f"{SYNC_BEGIN}{CURSOR_HIDE}{CURSOR_SAVE}{body}{CURSOR_RESTORE}{CURSOR_SHOW}{SYNC_END}"


PHASE_GRADIENT: list[str] = [
    "" if _NO_COLOR else "\033[38;2;30;65;187m",  # solid deep blue
    "" if _NO_COLOR else "\033[38;2;40;105;204m",  # medium blue
    "" if _NO_COLOR else "\033[38;2;55;150;221m",  # teal-blue
    "" if _NO_COLOR else "\033[38;2;75;200;238m",  # muted cyan
    "" if _NO_COLOR else "\033[38;2;102;255;255m",  # bright neon cyan
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
    "",  # 0: unused
    "" if _NO_COLOR else "\033[38;2;0;51;14m",  # 1: darkest
    "" if _NO_COLOR else "\033[38;2;0;71;20m",  # 2
    "" if _NO_COLOR else "\033[38;2;0;92;26m",  # 3
    "" if _NO_COLOR else "\033[38;2;0;117;33m",  # 4
    "" if _NO_COLOR else "\033[38;2;0;143;40m",  # 5
    "" if _NO_COLOR else "\033[38;2;0;173;49m",  # 6
    "" if _NO_COLOR else "\033[38;2;0;204;58m",  # 7
    "" if _NO_COLOR else "\033[38;2;0;230;65m",  # 8: peak saturated
]

PULSE_DIM: str = "" if _NO_COLOR else "\033[38;2;0;51;14m"


def _rgb(r: int, g: int, b: int) -> str:
    return "" if _NO_COLOR else color_code((r, g, b))


THEMES: dict[str, dict] = {
    "default": {
        "phase": [
            (13, 115, 42),
            (16, 147, 53),
            (20, 179, 65),
            (23, 211, 77),
            (39, 231, 94),
        ],
        "pulse": [
            (0, 61, 17),
            (0, 82, 23),
            (0, 102, 29),
            (0, 128, 36),
            (0, 153, 43),
            (0, 178, 51),
            (0, 204, 58),
            (0, 230, 65),
            (0, 250, 71),
        ],
        "title_wave": [
            None,
            (0, 51, 14),
            (0, 71, 20),
            (0, 92, 26),
            (0, 117, 33),
            (0, 143, 40),
            (0, 173, 49),
            (0, 204, 58),
            (0, 230, 65),
        ],
        "pulse_dim": (0, 51, 14),
        "idle_wave": ((0, 71, 20), (0, 163, 46), (0, 255, 72)),
        "accent": "\033[38;2;0;204;58m",
        "accent_hi": "\033[38;2;0;250;71m",
        "border": (30, 62, 39),
    },
    "plum": {
        "phase": [
            (96, 0, 128),
            (122, 0, 163),
            (149, 0, 199),
            (176, 0, 235),
            (195, 15, 255),
        ],
        "pulse": [
            (46, 0, 61),
            (61, 0, 82),
            (77, 0, 102),
            (96, 0, 128),
            (115, 0, 153),
            (134, 0, 178),
            (153, 0, 204),
            (172, 0, 230),
            (187, 0, 250),
        ],
        "title_wave": [
            None,
            (38, 0, 51),
            (54, 0, 71),
            (69, 0, 92),
            (88, 0, 117),
            (107, 0, 143),
            (130, 0, 173),
            (153, 0, 204),
            (172, 0, 230),
        ],
        "pulse_dim": (38, 0, 51),
        "idle_wave": ((54, 0, 71), (122, 0, 163), (191, 0, 255)),
        "accent": "\033[38;2;153;0;204m",
        "accent_hi": "\033[38;2;187;0;250m",
        "border": (54, 30, 62),
    },
    "lemon": {
        "phase": [
            (118, 109, 10),
            (151, 139, 12),
            (184, 170, 15),
            (217, 200, 18),
            (237, 220, 33),
        ],
        "pulse": [
            (61, 56, 0),
            (82, 75, 0),
            (102, 94, 0),
            (128, 117, 0),
            (153, 140, 0),
            (178, 164, 0),
            (204, 187, 0),
            (230, 210, 0),
            (250, 229, 0),
        ],
        "title_wave": [
            None,
            (51, 47, 0),
            (71, 65, 0),
            (92, 84, 0),
            (117, 108, 0),
            (143, 131, 0),
            (173, 159, 0),
            (204, 187, 0),
            (230, 210, 0),
        ],
        "pulse_dim": (51, 47, 0),
        "idle_wave": ((71, 65, 0), (163, 150, 0), (255, 234, 0)),
        "accent": "\033[38;2;204;187;0m",
        "accent_hi": "\033[38;2;250;229;0m",
        "border": (62, 59, 30),
    },
    "blueberry": {
        "phase": [
            (10, 64, 118),
            (12, 82, 151),
            (15, 99, 184),
            (18, 117, 217),
            (33, 135, 237),
        ],
        "pulse": [
            (0, 31, 61),
            (0, 41, 82),
            (0, 51, 102),
            (0, 64, 128),
            (0, 76, 153),
            (0, 89, 178),
            (0, 102, 204),
            (0, 115, 230),
            (0, 125, 250),
        ],
        "title_wave": [
            None,
            (0, 25, 51),
            (0, 36, 71),
            (0, 46, 92),
            (0, 59, 117),
            (0, 71, 143),
            (0, 87, 173),
            (0, 102, 204),
            (0, 115, 230),
        ],
        "pulse_dim": (0, 25, 51),
        "idle_wave": ((0, 36, 71), (0, 82, 163), (0, 127, 255)),
        "accent": "\033[38;2;0;102;204m",
        "accent_hi": "\033[38;2;0;125;250m",
        "border": (30, 46, 62),
    },
    "grape": {
        "phase": [
            (71, 19, 108),
            (91, 24, 139),
            (111, 30, 169),
            (131, 35, 199),
            (149, 51, 219),
        ],
        "pulse": [
            (35, 6, 55),
            (46, 8, 73),
            (58, 10, 92),
            (72, 13, 115),
            (87, 15, 138),
            (101, 18, 161),
            (116, 20, 184),
            (130, 23, 207),
            (142, 25, 225),
        ],
        "title_wave": [
            None,
            (29, 5, 46),
            (40, 7, 64),
            (52, 9, 83),
            (66, 12, 106),
            (81, 14, 129),
            (98, 17, 156),
            (116, 20, 184),
            (130, 23, 207),
        ],
        "pulse_dim": (29, 5, 46),
        "idle_wave": ((40, 7, 64), (92, 16, 147), (144, 25, 230)),
        "accent": "\033[38;2;116;20;184m",
        "accent_hi": "\033[38;2;142;25;225m",
        "border": (48, 33, 59),
    },
    "pear": {
        "phase": [
            (41, 86, 47),
            (53, 110, 61),
            (65, 134, 74),
            (76, 158, 87),
            (93, 177, 104),
        ],
        "pulse": [
            (20, 41, 23),
            (27, 55, 30),
            (33, 69, 38),
            (41, 86, 47),
            (50, 103, 57),
            (58, 120, 66),
            (66, 138, 76),
            (75, 155, 85),
            (81, 169, 93),
        ],
        "title_wave": [
            None,
            (17, 34, 19),
            (23, 48, 27),
            (30, 62, 34),
            (38, 79, 44),
            (46, 96, 53),
            (56, 117, 64),
            (66, 138, 76),
            (75, 155, 85),
        ],
        "pulse_dim": (17, 34, 19),
        "idle_wave": ((23, 48, 27), (53, 110, 61), (83, 172, 95)),
        "accent": "\033[38;2;66;138;76m",
        "accent_hi": "\033[38;2;81;169;93m",
        "border": (39, 53, 41),
    },
    "acai": {
        "phase": [
            (10, 69, 118),
            (12, 89, 151),
            (15, 108, 184),
            (18, 127, 217),
            (33, 145, 237),
        ],
        "pulse": [
            (4, 33, 58),
            (5, 44, 77),
            (6, 55, 96),
            (8, 69, 120),
            (9, 83, 144),
            (11, 97, 168),
            (12, 111, 192),
            (14, 125, 216),
            (15, 136, 235),
        ],
        "title_wave": [
            None,
            (3, 28, 48),
            (4, 39, 67),
            (6, 50, 86),
            (7, 64, 110),
            (9, 78, 134),
            (10, 94, 163),
            (12, 111, 192),
            (14, 125, 216),
        ],
        "pulse_dim": (3, 28, 48),
        "idle_wave": ((4, 39, 67), (10, 89, 153), (15, 139, 240)),
        "accent": "\033[38;2;12;111;192m",
        "accent_hi": "\033[38;2;15;136;235m",
        "border": (32, 47, 60),
    },
    "tangerine": {
        "phase": [
            (121, 31, 6),
            (155, 40, 8),
            (189, 49, 10),
            (223, 57, 12),
            (243, 74, 27),
        ],
        "pulse": [
            (59, 15, 2),
            (78, 20, 3),
            (98, 24, 4),
            (122, 31, 5),
            (147, 37, 6),
            (171, 43, 7),
            (196, 49, 8),
            (220, 55, 9),
            (240, 60, 10),
        ],
        "title_wave": [
            None,
            (49, 12, 2),
            (69, 17, 3),
            (88, 22, 4),
            (113, 28, 5),
            (137, 34, 6),
            (166, 42, 7),
            (196, 49, 8),
            (220, 55, 9),
        ],
        "pulse_dim": (49, 12, 2),
        "idle_wave": ((69, 17, 3), (157, 39, 7), (245, 61, 10)),
        "accent": "\033[38;2;196;49;8m",
        "accent_hi": "\033[38;2;240;60;10m",
        "border": (61, 38, 31),
    },
    "lime": {
        "phase": [
            (64, 118, 10),
            (82, 151, 12),
            (99, 184, 15),
            (117, 217, 18),
            (135, 237, 33),
        ],
        "pulse": [
            (31, 61, 0),
            (41, 82, 0),
            (51, 102, 0),
            (64, 128, 0),
            (77, 153, 0),
            (89, 178, 0),
            (102, 204, 0),
            (115, 230, 0),
            (125, 250, 0),
        ],
        "title_wave": [
            None,
            (26, 51, 0),
            (36, 71, 0),
            (46, 92, 0),
            (59, 117, 0),
            (71, 143, 0),
            (87, 173, 0),
            (102, 204, 0),
            (115, 230, 0),
        ],
        "pulse_dim": (26, 51, 0),
        "idle_wave": ((36, 71, 0), (82, 163, 0), (128, 255, 0)),
        "accent": "\033[38;2;102;204;0m",
        "accent_hi": "\033[38;2;125;250;0m",
        "border": (46, 62, 30),
    },
}

THEME_NAMES: list[str] = list(THEMES.keys())

IDLE_WAVE_COLORS: tuple = THEMES["default"]["idle_wave"]

# (dimmest, brightest) endpoints of the bar-wave lightness ramp. Both are taken
# from the theme's own ``pulse`` gradient, so every shade blended between them
# stays on the theme hue instead of drifting toward grey.
WAVE_SHADES: tuple = (THEMES["default"]["pulse"][0], THEMES["default"]["pulse"][-1])

ACCENT: str = "" if _NO_COLOR else "\033[38;2;0;204;58m"
ACCENT_HI: str = "" if _NO_COLOR else "\033[38;2;0;250;71m"
BORDER: str = "" if _NO_COLOR else "\033[38;2;30;62;39m"


def apply_theme(name: str) -> None:
    """Apply a named color theme, updating all module-level gradient variables."""
    global PHASE_GRADIENT, PULSE_GRADIENT, TITLE_WAVE_GRADIENT, PULSE_DIM
    global IDLE_WAVE_COLORS, WAVE_SHADES, ACCENT, ACCENT_HI, BORDER

    theme = THEMES.get(name, THEMES["default"])

    PHASE_GRADIENT[:] = [_rgb(*c) for c in theme["phase"]]
    PULSE_GRADIENT[:] = [_rgb(*c) for c in theme["pulse"]]
    TITLE_WAVE_GRADIENT[:] = ["" if c is None else _rgb(*c) for c in theme["title_wave"]]
    PULSE_DIM = _rgb(*theme["pulse_dim"])
    IDLE_WAVE_COLORS = theme["idle_wave"]
    WAVE_SHADES = (theme["pulse"][0], theme["pulse"][-1])
    ACCENT = "" if _NO_COLOR else theme["accent"]
    ACCENT_HI = "" if _NO_COLOR else theme["accent_hi"]
    BORDER = _rgb(*theme["border"])


_ok = f"{S.HGRN}✓{S.RST}"
_fail = f"{S.HRED}✗{S.RST}"
_wait = f"{S.HYEL}●{S.RST}"
_work = f"{S.HCYN}◌{S.RST}"
_skip = f"{S.DIM}○{S.RST}"
_arrow = f"{S.DIM}→{S.RST}"
_dot = f"{S.DIM}·{S.RST}"
_tri = f"{S.DIM}▸{S.RST}"

_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _vlen(text: str) -> int:
    """Visible length of *text*, ignoring ANSI escape sequences."""
    return len(re.sub(r"\033\[[0-9;]*m", "", str(text)))


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
        seg = ch * 2 + f" {S.RST}{S.BOLD}{ACCENT}{label}{S.RST}{BORDER} " + ch * max(1, w - vis - 4)
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
        return (
            f"  {BORDER}{lc}{ch} {S.RST}{S.BOLD}{ACCENT}{title}{S.RST}"
            f"{BORDER} {ch * fill}{rc}{S.RST}"
        )
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
        return f"  {BORDER}├─ {S.RST}{S.BOLD}{ACCENT}{label}{S.RST}{BORDER} {'─' * fill}┤{S.RST}"
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
    return "\n".join(
        [
            f"  {BORDER}┏{'━' * (width - 2)}┓{S.RST}",
            f"  {BORDER}┃{S.RST} {' ' * lp}{S.BOLD}{ACCENT}{title}{S.RST}"
            f"{' ' * rp} {BORDER}┃{S.RST}",
            f"  {BORDER}┗{'━' * (width - 2)}┛{S.RST}",
        ]
    )


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
    return text if len(text) <= length else text[: length - 1] + "…"


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
