"""Braille wave math — pure rendering functions.

Three kinds of animated braille waves are produced here:

  - ``_title_wave`` — short compact wave for the "Generating" box title.
  - ``_render_pulse_bar`` — token-progress bar that fills as output streams;
    scroll speed is driven by *phase* (accumulated externally) so it tracks
    throughput. Falls back to ``_render_pre_wave_bar`` for empty space.
  - ``_render_pre_wave_bar`` — short reasoning-state wave (1–3 dots high)
    shown before the first completion token arrives.
  - ``render_idle_wave`` — layered full-width ocean used on the idle menu
    background; *intensity* (0.0–1.0) controls amplitude, speed, and color.
    A filled foreground and two parallax contours provide depth without
    overlapping differently colored braille fills.

No state, no I/O — just functions that map (tick, width, height, intensity)
to strings. The module is consumed by ``tracker`` (for live rendering) and
directly by ``__main__`` (for the idle-menu background).
"""

from __future__ import annotations

import math

from wavebench.tui import styles as _styles
from wavebench.tui.styles import (
    _NO_COLOR,
    PULSE_GRADIENT,
    TITLE_WAVE_GRADIENT,
    S,
)

BAR_WIDTH = 20

_WAVE_CHARS: list = [
    ["⠀"],  # 0: empty
    ["⡀", "⢀"],  # 1: single bottom dot
    ["⣀"],  # 2: full bottom row
    ["⣄", "⣠"],  # 3: bottom row + half of row 3
    ["⣤"],  # 4: bottom 2 rows
    ["⣦", "⣴"],  # 5: bottom 2 rows + half of row 2
    ["⣶"],  # 6: bottom 3 rows
    ["⣷", "⣾"],  # 7: bottom 3 rows + half of top
    ["⣿"],  # 8: full block
]


def _title_wave(tick: int, width: int = 5) -> str:
    """Animated braille wave for the 'Generating' box title."""
    parts: list[str] = []
    prev_level = -1
    for i in range(width):
        val = math.sin(tick * 0.15 - i * 0.7) * 0.5 + 0.5
        level = max(1, min(8, round(val * 8)))
        pool = _WAVE_CHARS[level]
        ch = pool[(i + tick) % len(pool)]
        if not _NO_COLOR and level != prev_level:
            if prev_level >= 0:
                parts.append(S.RST)
            parts.append(TITLE_WAVE_GRADIENT[level])
            prev_level = level
        parts.append(ch)
    if not _NO_COLOR and prev_level >= 0:
        parts.append(S.RST)
    return "".join(parts)


def _render_pulse_bar(
    chars: int, scale: float, tick: int, phase: float = 0.0, bar_width: int = BAR_WIDTH
) -> str:
    """Animated braille wave progress bar.

    Height builds from the bottom row of each braille cell upward
    following a sine wave that scrolls right.  *phase* is accumulated
    externally (driven by token rate) so scroll speed tracks throughput.
    Amplitude scales with token progress so the wave grows as output fills.
    """
    ratio = min(chars / max(scale, 1), 1.0)
    filled = round(ratio * bar_width)
    empty = bar_width - filled

    t = tick * 0.008
    bandwidth = 0.70
    amplitude = (0.35 + 0.65 * ratio) * (0.92 + 0.08 * math.sin(t * 1.7 + 2.0))
    parts: list[str] = []
    prev_level = -1

    for i in range(filled):
        w = math.sin(i * bandwidth - phase)
        w2 = math.sin(i * bandwidth * 1.8 - phase * 0.6 + 1.2) * 0.25
        val = max(0.0, min(1.0, (w + w2) * 0.5 + 0.5)) * amplitude
        val = max(0.12, val)

        edge = filled - 1 - i
        if edge < 3 and filled > 4:
            val = min(1.0, val + (3 - edge) * 0.15)

        level = max(1, min(8, round(val * 8)))

        pool = _WAVE_CHARS[level]
        ch = pool[(i + tick) % len(pool)]

        if level != prev_level:
            if prev_level >= 0:
                parts.append(S.RST)
            parts.append(PULSE_GRADIENT[level])
            prev_level = level
        parts.append(ch)

    if filled:
        parts.append(S.RST)

    if empty > 0:
        parts.append(_render_pre_wave_bar(empty, tick))

    return "".join(parts)


def _render_pre_wave_bar(width: int, tick: int) -> str:
    """Mini braille wave shown during pre-generation 'reasoning' state.

    Same wave style as the main progress bar but constrained to 1-3 dots
    high, with a per-character gradient from bright green (matching the
    leading edge of the main bar) to grey-blue, pulsing over time.
    """
    parts: list[str] = []
    t = tick * 0.06
    phase = tick * 0.10

    _pw = _styles.PRE_WAVE_COLORS
    _s0, _s1 = _pw["start_base"], _pw["start_amp"]
    _t0, _t1 = _pw["target_base"], _pw["target_amp"]

    for i in range(width):
        pos = i / max(width - 1, 1)

        w = math.sin(i * 0.6 - phase)
        w2 = math.sin(i * 1.05 - phase * 0.65 + 1.3) * 0.28
        val = max(0.0, min(1.0, (w + w2) * 0.5 + 0.5))
        level = max(1, min(3, round(val * 3)))

        pool = _WAVE_CHARS[level]
        ch = pool[(i + tick) % len(pool)]

        if _NO_COLOR:
            parts.append(ch)
            continue

        p1 = math.sin(t + i * 0.35) * 0.5 + 0.5
        p2 = math.sin(t * 0.6 + i * 0.2 + 2.0) * 0.3 + 0.5
        p = p1 * 0.65 + p2 * 0.35

        sr = _s0[0] + p * _s1[0]
        sg = _s0[1] + p * _s1[1]
        sb = _s0[2] + p * _s1[2]
        tr = _t0[0] + p * _t1[0]
        tg = _t0[1] + p * _t1[1]
        tb = _t0[2] + p * _t1[2]

        r = max(0, min(255, int(sr + (tr - sr) * pos)))
        g = max(0, min(255, int(sg + (tg - sg) * pos)))
        b = max(0, min(255, int(sb + (tb - sb) * pos)))

        parts.append(f"\033[38;2;{r};{g};{b}m{ch}")

    if not _NO_COLOR:
        parts.append(S.RST)
    return "".join(parts)


def _shape_wave(value: float, stokes: float, crest_exp: float, limiter: float) -> float:
    """Sharpen positive crests while leaving troughs broad and rounded."""
    value += stokes * value * value
    if value > 0:
        value = value**crest_exp
        return value / (1.0 + limiter * value)
    return -(abs(value) ** 1.3)


def _idle_wave_surfaces(
    tick: int,
    width: int,
    height: int,
    intensity: float,
    wave_phase: float | None,
) -> tuple[list[float], list[float], list[float]]:
    """Build wave surfaces on a grid of braille dot columns and rows."""
    if width <= 0 or height <= 0:
        return [], [], []

    intensity = max(0.0, min(1.0, intensity))
    total_sp = height * 4

    amp_scale = 0.14 + 0.24 * intensity
    amp_breath = 0.06 + 0.12 * intensity
    amp = total_sp * amp_scale * (1.0 + amp_breath * math.sin(tick * 0.019 + 1.0))

    center_norm = 0.75 - 0.20 * intensity
    center_sway = 0.12 + 0.22 * intensity
    center = total_sp * center_norm + amp * center_sway * math.sin(tick * 0.024)
    depth_gap = total_sp * (0.085 + 0.015 * intensity)

    if wave_phase is None:
        wave_phase = tick * (0.35 + 0.75 * intensity)

    stokes = 0.06 + 0.18 * intensity
    crest_exp = 1.3 + 0.5 * intensity
    limiter = 0.35 - 0.15 * intensity

    far: list[float] = []
    middle: list[float] = []
    foreground: list[float] = []
    for col in range(width):
        nx = col / max(width - 1, 1)

        far_height = (
            0.72 * math.sin(nx * 18.0 - wave_phase * 0.045 + 0.8)
            + 0.12 * math.sin(nx * 31.0 - wave_phase * 0.064 + 2.4)
            + 0.03 * math.sin(nx * 52.0 - wave_phase * 0.083 + 0.3)
        )
        far_height = _shape_wave(
            far_height,
            stokes * 0.30,
            1.10 + 0.15 * intensity,
            limiter + 0.16,
        )
        far.append(center - depth_gap * 2.0 - far_height * amp * 0.36)

        middle_height = (
            0.67 * math.sin(nx * 15.8 - wave_phase * 0.074 + 0.3)
            + (0.08 + 0.08 * intensity)
            * math.sin(nx * 28.0 - wave_phase * 0.103 + 2.0)
            + (0.02 + 0.04 * intensity)
            * math.sin(nx * 47.0 - wave_phase * 0.132 + 3.4)
        )
        middle_height = _shape_wave(
            middle_height,
            stokes * 0.62,
            1.18 + 0.28 * intensity,
            limiter + 0.08,
        )
        middle.append(center - depth_gap - middle_height * amp * 0.65)

        foreground_height = (
            0.62 * math.sin(nx * 14.0 - wave_phase * 0.107)
            + (0.08 + 0.14 * intensity)
            * math.sin(nx * 26.0 - wave_phase * 0.147 + 1.7)
            + (0.02 + 0.08 * intensity)
            * math.sin(nx * 44.0 - wave_phase * 0.187 + 3.1)
            + (0.01 + 0.03 * intensity)
            * math.sin(nx * 68.0 - wave_phase * 0.240 + 0.9)
        )
        foreground_height = _shape_wave(
            foreground_height,
            stokes,
            crest_exp,
            limiter,
        )
        foreground.append(center - foreground_height * amp)

    return far, middle, foreground


def _blend_color(
    start: tuple[int, int, int], end: tuple[int, int, int], amount: float
) -> tuple[int, int, int]:
    amount = max(0.0, min(1.0, amount))
    return tuple(round(a + (b - a) * amount) for a, b in zip(start, end, strict=True))


def _scale_color(color: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, round(channel * amount))) for channel in color)


_BRAILLE_DOT_BITS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)
_BRAILLE_FILL_MASKS = (
    (0x47, 0x46, 0x44, 0x40, 0x00),
    (0xB8, 0xB0, 0xA0, 0x80, 0x00),
)


def _surface_fill_mask(surface: list[float], row: int, col: int) -> int:
    """Return the foreground-water mask for one 2×4 braille cell."""
    cell_top = row * 4
    mask = 0
    for subcol in range(2):
        first_wet_dot = math.ceil(surface[col * 2 + subcol] - 0.5) - cell_top
        first_wet_dot = max(0, min(4, first_wet_dot))
        mask |= _BRAILLE_FILL_MASKS[subcol][first_wet_dot]
    return mask


def _surface_contour_mask(
    surface: list[float],
    row: int,
    col: int,
    occluding_surface: list[float] | None = None,
) -> int:
    """Return a two-dot-wide contour, clipped behind a nearer surface."""
    mask = 0
    for subcol in range(2):
        index = col * 2 + subcol
        if occluding_surface is not None and surface[index] >= occluding_surface[index]:
            continue
        dot_y = math.floor(surface[index])
        if dot_y >= 0 and dot_y // 4 == row:
            mask |= _BRAILLE_DOT_BITS[dot_y % 4][subcol]
    return mask


def _color_code(color: tuple[int, int, int]) -> str:
    return f"\033[38;2;{color[0]};{color[1]};{color[2]}m"


def render_idle_wave(
    tick: int, width: int, height: int, intensity: float = 0.0, wave_phase: float | None = None
) -> list[str]:
    """Render a filled foreground wave with two parallax depth contours.

    Returns *height* ANSI-colored strings, each *width* visible characters
    wide.  Each surface is sampled at the braille cell's true 2×4 resolution.
    Rear surfaces are contours rather than overlapping fills because a terminal
    cell cannot assign separate colors to different dots in one braille glyph.
    """
    if _NO_COLOR or height <= 0 or width <= 0:
        return [" " * width] * max(height, 0)

    intensity = max(0.0, min(1.0, intensity))
    far_surface, middle_surface, foreground_surface = _idle_wave_surfaces(
        tick, width * 2, height, intensity, wave_phase
    )
    far_occluding_surface = [
        min(middle_y, foreground_y)
        for middle_y, foreground_y in zip(
            middle_surface,
            foreground_surface,
            strict=True,
        )
    ]

    color_t = max(0.0, min(1.0, intensity + 0.03 * math.sin(tick * 0.03)))
    low, middle, high = _styles.IDLE_WAVE_COLORS
    if color_t < 0.5:
        active_color = _blend_color(low, middle, color_t * 2.0)
    else:
        active_color = _blend_color(middle, high, (color_t - 0.5) * 2.0)

    far_color = _scale_color(_blend_color(low, active_color, 0.22), 0.52 + 0.08 * intensity)
    middle_color = _scale_color(
        _blend_color(low, active_color, 0.62), 0.72 + 0.08 * intensity
    )
    far_contour_code = _color_code(
        _scale_color(_blend_color(far_color, middle_color, 0.25), 1.05)
    )
    middle_contour_code = _color_code(_blend_color(middle_color, active_color, 0.28))

    rows: list[str] = []
    for row in range(height):
        row_depth = row / max(height - 1, 1)
        foreground_body_code = _color_code(
            _scale_color(active_color, 1.0 - 0.42 * row_depth)
        )
        parts: list[str] = []
        current_color: str | None = None

        for col in range(width):
            foreground_mask = _surface_fill_mask(foreground_surface, row, col)
            if foreground_mask:
                mask = foreground_mask
                color = foreground_body_code
            else:
                middle_mask = _surface_contour_mask(
                    middle_surface,
                    row,
                    col,
                    occluding_surface=foreground_surface,
                )
                if middle_mask:
                    mask = middle_mask
                    color = middle_contour_code
                else:
                    mask = _surface_contour_mask(
                        far_surface,
                        row,
                        col,
                        occluding_surface=far_occluding_surface,
                    )
                    color = far_contour_code

            if not mask:
                if current_color is not None:
                    parts.append(S.RST)
                    current_color = None
                parts.append(" ")
                continue

            if color != current_color:
                parts.append(color)
                current_color = color
            parts.append(chr(0x2800 + mask))

        if current_color is not None:
            parts.append(S.RST)
        rows.append("".join(parts))

    return rows
