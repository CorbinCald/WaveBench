"""Braille wave math — pure rendering functions.

Three kinds of animated braille waves are produced here:

  - ``_title_wave`` — short compact wave for the "Generating" box title.
  - ``_render_pulse_bar`` — token-progress bar that fills as output streams;
    scroll speed is driven by *phase* (accumulated externally) so it tracks
    throughput. Falls back to ``_render_pre_wave_bar`` for empty space.
  - ``_render_pre_wave_bar`` — short reasoning-state wave (1–3 dots high)
    shown before the first completion token arrives.
  - ``render_idle_wave`` — full-width ocean wave used on the idle menu
    background; *intensity* (0.0–1.0) controls amplitude, speed, and color.

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


def render_idle_wave(
    tick: int, width: int, height: int, intensity: float = 0.0, wave_phase: float | None = None
) -> list[str]:
    """Render one frame of an animated ocean wave.

    Returns *height* ANSI-colored strings, each *width* visible characters
    wide.  *intensity* (0.0--1.0) controls wave energy: 0.0 gives a calm,
    low swell; 1.0 gives tall, fast, sharply cresting waves.  All wave
    physics -- amplitude, phase speed, harmonic content, Stokes nonlinearity,
    and gradient color -- interpolate smoothly between those extremes.
    """
    if _NO_COLOR or height <= 0 or width <= 0:
        return [" " * width] * max(height, 0)

    intensity = max(0.0, min(1.0, intensity))
    total_sp = height * 8

    amp_scale = 0.14 + 0.24 * intensity
    amp_breath = 0.06 + 0.12 * intensity
    amp = total_sp * amp_scale * (1.0 + amp_breath * math.sin(tick * 0.019 + 1.0))

    center_norm = 0.75 - 0.20 * intensity
    center_sway = 0.12 + 0.22 * intensity
    center = total_sp * center_norm + amp * center_sway * math.sin(tick * 0.024)

    spd = 0.35 + 0.75 * intensity
    if wave_phase is None:
        wave_phase = tick * spd

    h1_amp = 0.62
    h2_amp = 0.08 + 0.14 * intensity
    h3_amp = 0.02 + 0.08 * intensity
    h4_amp = 0.01 + 0.03 * intensity

    stokes = 0.06 + 0.18 * intensity
    crest_exp = 1.3 + 0.5 * intensity
    limiter = 0.35 - 0.15 * intensity

    ct = max(0.0, min(1.0, intensity + 0.03 * math.sin(tick * 0.03)))
    _low, _mid, _high = _styles.IDLE_WAVE_COLORS
    if ct < 0.5:
        f = ct * 2.0
        cr = _low[0] + (_mid[0] - _low[0]) * f
        cg = _low[1] + (_mid[1] - _low[1]) * f
        cb = _low[2] + (_mid[2] - _low[2]) * f
    else:
        f = (ct - 0.5) * 2.0
        cr = _mid[0] + (_high[0] - _mid[0]) * f
        cg = _mid[1] + (_high[1] - _mid[1]) * f
        cb = _mid[2] + (_high[2] - _mid[2]) * f
    wave_color = f"\033[38;2;{int(cr)};{int(cg)};{int(cb)}m"

    surfaces: list[float] = []
    for col in range(width):
        nx = col / max(width - 1, 1)
        h = (
            h1_amp * math.sin(nx * 14.0 - wave_phase * 0.107)
            + h2_amp * math.sin(nx * 26.0 - wave_phase * 0.147 + 1.7)
            + h3_amp * math.sin(nx * 44.0 - wave_phase * 0.187 + 3.1)
            + h4_amp * math.sin(nx * 68.0 - wave_phase * 0.240 + 0.9)
        )
        h += stokes * h * h
        if h > 0:
            h = h**crest_exp
            h /= 1.0 + limiter * h
        else:
            h = -(abs(h) ** 1.3)
        surfaces.append(center - h * amp)

    rows: list[str] = []
    for row in range(height):
        cell_top = row * 8
        cell_bot = cell_top + 8
        parts: list[str] = []
        in_color = False

        for col in range(width):
            s = surfaces[col]

            if s <= cell_top:
                level = 8
            elif s >= cell_bot:
                level = 0
            else:
                level = max(1, min(7, round(cell_bot - s)))

            if level == 0:
                if in_color:
                    parts.append(S.RST)
                    in_color = False
                parts.append(" ")
                continue

            pool = _WAVE_CHARS[level]
            ch = pool[(col + tick) % len(pool)]

            if not in_color:
                parts.append(wave_color)
                in_color = True
            parts.append(ch)

        if in_color:
            parts.append(S.RST)
        rows.append("".join(parts))

    return rows
