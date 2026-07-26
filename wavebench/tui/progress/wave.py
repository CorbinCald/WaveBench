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

Every wave here is hue-locked: a single theme color varied in lightness only.
Wave height is carried by the braille glyph, depth and progress by brightness,
so a wave never changes hue mid-gradient. Two things break that if reintroduced
— adding equal amounts to R/G/B (washes toward white/grey) and keying color off
the quantised height *level* (bands adjacent cells instead of blending them).

No state, no I/O — just functions that map (tick, width, height, intensity)
to strings. The module is consumed by ``tracker`` (for live rendering) and
directly by ``__main__`` (for the idle-menu background).
"""

from __future__ import annotations

import math

from wavebench.tui import styles as _styles
from wavebench.tui.styles import (
    _NO_COLOR,
    TITLE_WAVE_GRADIENT,
    S,
)

BAR_WIDTH = 20

# Darkest shade a bar wave is allowed to fade to, as a position along the
# theme's lightness ramp. Braille glyphs only light ~1/3 of a cell, so a bar
# that fades all the way to the ramp's floor reads as unlit rather than dim.
BAR_SHADE_FLOOR = 0.18

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


def _blend_color(
    start: tuple[int, int, int], end: tuple[int, int, int], amount: float
) -> tuple[int, int, int]:
    amount = max(0.0, min(1.0, amount))
    return tuple(round(a + (b - a) * amount) for a, b in zip(start, end, strict=True))


def _scale_color(color: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, round(channel * amount))) for channel in color)


def _color_code(color: tuple[int, int, int]) -> str:
    return _styles.color_code(color)


def _bar_shade(index: int, crest: int, span: int, tick: int) -> str:
    """Colour one character of a bar wave from the theme's lightness ramp.

    Brightness peaks at *crest* — the wave's leading edge — and falls off with
    distance over *span* characters, so the generated section and the trailing
    pre-wave form a single continuous gradient rather than two palettes meeting
    at a seam. Both ramp endpoints share the theme hue, so only lightness moves.
    """
    # Reach spans the whole bar so the ramp never flattens out into a run of
    # identical cells — every cell sits at its own point on the gradient.
    reach = max(span, 4.0)
    nearness = max(0.0, 1.0 - abs(index - crest) / reach)
    shade = BAR_SHADE_FLOOR + (1.0 - BAR_SHADE_FLOOR) * nearness
    shade *= 0.94 + 0.06 * math.sin(tick * 0.06 - index * 0.22)
    dim, bright = _styles.WAVE_SHADES
    return _color_code(_blend_color(dim, bright, shade))


def _title_wave(tick: int, width: int = 5) -> str:
    """Animated braille wave for the 'Generating' box title."""
    parts: list[str] = []
    prev_level = -1
    for i in range(width):
        # A broad arc reads more like a rolling swell than a tiny sawtooth in
        # the five-character title. Keep the full height range, but spread the
        # rise over more cells so adjacent glyphs change gradually.
        val = math.sin(tick * 0.13 - i * 0.48) * 0.44 + 0.5
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

    Colour is a single-hue lightness ramp peaking at the leading edge and
    continuing unbroken into the trailing pre-wave, so the whole bar reads as
    one gradient. Wave *height* is carried by the glyph alone, never by hue.
    """
    ratio = min(chars / max(scale, 1), 1.0)
    filled = round(ratio * bar_width)
    empty = bar_width - filled

    t = tick * 0.008
    bandwidth = 0.44
    amplitude = (0.35 + 0.65 * ratio) * (0.92 + 0.08 * math.sin(t * 1.7 + 2.0))
    parts: list[str] = []
    crest = filled - 1
    prev_color: str | None = None

    for i in range(filled):
        w = math.sin(i * bandwidth - phase)
        w2 = math.sin(i * bandwidth * 1.55 - phase * 0.58 + 1.2) * 0.16
        val = max(0.0, min(1.0, (w + w2) * 0.48 + 0.5)) * amplitude
        val = max(0.12, val)

        level = max(1, min(8, round(val * 8)))
        if empty:
            # Ease the tall generated wave into the low trailing swell. The
            # old three-cell crest boost ended in a sheer 5-dot cliff. This
            # smoothstep taper keeps the progress edge distinct through color
            # while making its silhouette continuous.
            edge_distance = filled - i
            tall_weight = min(1.0, edge_distance / 5.0)
            tall_weight = tall_weight * tall_weight * (3.0 - 2.0 * tall_weight)
            trailing_level = _pre_wave_level(i, tick)
            level = round(trailing_level + (level - trailing_level) * tall_weight)

        pool = _WAVE_CHARS[level]
        ch = pool[(i + tick) % len(pool)]

        if not _NO_COLOR:
            color = _bar_shade(i, crest, bar_width, tick)
            if color != prev_color:
                parts.append(color)
                prev_color = color
        parts.append(ch)

    if filled and not _NO_COLOR:
        parts.append(S.RST)

    if empty > 0:
        parts.append(_render_pre_wave_bar(empty, tick, crest=-1, span=bar_width, offset=filled))

    return "".join(parts)


def _pre_wave_level(index: int, tick: int) -> int:
    """Return the gently rolling 1–3 dot height of the reasoning swell."""
    phase = tick * 0.085
    w = math.sin(index * 0.43 - phase)
    w2 = math.sin(index * 0.76 - phase * 0.62 + 1.3) * 0.18
    val = max(0.0, min(1.0, (w + w2) * 0.48 + 0.5))
    return max(1, min(3, round(val * 3)))


def _render_pre_wave_bar(
    width: int, tick: int, crest: int = 0, span: int | None = None, offset: int = 0
) -> str:
    """Mini braille wave shown during pre-generation 'reasoning' state.

    Same wave style as the main progress bar but constrained to 1-3 dots high.
    Colour comes from the shared single-hue ramp in :func:`_bar_shade`, so a
    standalone reasoning bar (*crest* 0) fades brightest-to-dimmest left to
    right, while the trailing section of a progress bar continues that bar's
    gradient — *offset* shifts it into the parent bar's coordinate space and
    *crest* points back at the leading edge just behind it.
    """
    parts: list[str] = []
    span = width if span is None else span
    prev_color: str | None = None

    for i in range(width):
        index = offset + i
        level = _pre_wave_level(index, tick)

        pool = _WAVE_CHARS[level]
        ch = pool[(index + tick) % len(pool)]

        if _NO_COLOR:
            parts.append(ch)
            continue

        color = _bar_shade(index, offset + crest, span, tick)
        if color != prev_color:
            parts.append(color)
            prev_color = color
        parts.append(ch)

    if not _NO_COLOR:
        parts.append(S.RST)
    return "".join(parts)


def _shape_wave(value: float, stokes: float, crest_exp: float, limiter: float) -> float:
    """Give a swell mild asymmetry while keeping its crests rounded."""
    value += stokes * value * value
    if value > 0:
        value = value**crest_exp
        return value / (1.0 + limiter * value)
    return -(abs(value) ** 1.12)


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

    # Braille cells are sampled at 2-by-4 dots. On an unusually tall/narrow
    # terminal, damp the amplitude so the same curve cannot become a wall.
    aspect_damping = min(1.0, width / max(total_sp * 2.2, 1.0))
    amp_scale = 0.12 + 0.15 * intensity
    amp_breath = 0.04 + 0.07 * intensity
    amp = total_sp * amp_scale * aspect_damping * (1.0 + amp_breath * math.sin(tick * 0.019 + 1.0))

    center_norm = 0.74 - 0.15 * intensity
    center_sway = 0.08 + 0.10 * intensity
    center = total_sp * center_norm + amp * center_sway * math.sin(tick * 0.024)
    depth_gap = total_sp * (0.085 + 0.010 * intensity)

    if wave_phase is None:
        wave_phase = tick * (0.35 + 0.75 * intensity)

    stokes = 0.025 + 0.065 * intensity
    crest_exp = 1.06 + 0.12 * intensity
    limiter = 0.28 + 0.04 * (1.0 - intensity)

    far: list[float] = []
    middle: list[float] = []
    foreground: list[float] = []
    for col in range(width):
        nx = col / max(width - 1, 1)

        far_height = (
            0.72 * math.sin(nx * 12.2 - wave_phase * 0.038 + 0.8)
            + 0.08 * math.sin(nx * 22.0 - wave_phase * 0.052 + 2.4)
            + 0.015 * math.sin(nx * 34.0 - wave_phase * 0.068 + 0.3)
        )
        far_height = _shape_wave(
            far_height,
            stokes * 0.20,
            1.02 + 0.04 * intensity,
            limiter + 0.16,
        )
        far.append(center - depth_gap * 2.0 - far_height * amp * 0.34)

        middle_height = (
            0.68 * math.sin(nx * 11.2 - wave_phase * 0.061 + 0.3)
            + (0.055 + 0.035 * intensity) * math.sin(nx * 20.4 - wave_phase * 0.084 + 2.0)
            + (0.012 + 0.018 * intensity) * math.sin(nx * 32.0 - wave_phase * 0.108 + 3.4)
        )
        middle_height = _shape_wave(
            middle_height,
            stokes * 0.50,
            1.04 + 0.07 * intensity,
            limiter + 0.08,
        )
        middle.append(center - depth_gap - middle_height * amp * 0.62)

        foreground_height = (
            0.64 * math.sin(nx * 10.2 - wave_phase * 0.086)
            + (0.06 + 0.055 * intensity) * math.sin(nx * 18.8 - wave_phase * 0.116 + 1.7)
            + (0.015 + 0.030 * intensity) * math.sin(nx * 29.0 - wave_phase * 0.148 + 3.1)
        )
        foreground_height = _shape_wave(
            foreground_height,
            stokes,
            crest_exp,
            limiter,
        )
        foreground.append(center - foreground_height * amp)

    return far, middle, foreground


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


# Depth ramp for the three ocean layers, as a fraction of the active colour.
# Every layer is the same colour scaled down, so depth reads as lightness only.
# Both rear layers stay below the foreground surface highlight, preserving the
# front-to-back depth cue even where deep foreground water is darker.
_FAR_DEPTH = 0.42
_MIDDLE_DEPTH = 0.66
# The foreground gradient combines distance below its local surface with a
# smaller absolute top-to-bottom falloff. Six local bands are enough to read as
# water depth at braille resolution while keeping each frame compact for a PTY.
_DEPTH_BANDS = 6
_SURFACE_SHADE = 1.15
_DEEP_SHADE = 0.55
_SCREEN_DEPTH_FALLOFF = 0.16


def _surface_depth_band(
    surface: list[float],
    row: int,
    col: int,
    total_height: int,
) -> int:
    """Map one water cell to a lightness band below its local surface."""
    surface_y = (surface[col * 2] + surface[col * 2 + 1]) * 0.5
    cell_midpoint = row * 4 + 1.5
    depth = max(0.0, cell_midpoint - surface_y)
    water_column = max(total_height - surface_y, 4.0)
    depth_t = min(1.0, depth / water_column)
    # Smoothstep holds a soft highlight near the surface and avoids crowding
    # most of the available colors into the shallowest cells.
    depth_t = depth_t * depth_t * (3.0 - 2.0 * depth_t)
    return min(_DEPTH_BANDS - 1, round(depth_t * (_DEPTH_BANDS - 1)))


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

    # Distant water lifts slightly as the ocean gains energy, but never past
    # the layer in front of it.
    far_depth = _FAR_DEPTH + 0.06 * intensity
    middle_depth = _MIDDLE_DEPTH + 0.04 * intensity

    rows: list[str] = []
    for row in range(height):
        screen_depth = (row + 0.5) / height
        screen_shade = 1.0 - _SCREEN_DEPTH_FALLOFF * screen_depth
        foreground_depth_codes = tuple(
            _color_code(
                _scale_color(
                    active_color,
                    (_SURFACE_SHADE + (_DEEP_SHADE - _SURFACE_SHADE) * band / (_DEPTH_BANDS - 1))
                    * screen_shade,
                )
            )
            for band in range(_DEPTH_BANDS)
        )
        # Rear contours retain their distance cue while sharing the gentle
        # screen-depth attenuation applied to the foreground water.
        middle_contour_code = _color_code(_scale_color(active_color, middle_depth * screen_shade))
        far_contour_code = _color_code(_scale_color(active_color, far_depth * screen_shade))
        parts: list[str] = []
        current_color: str | None = None

        for col in range(width):
            foreground_mask = _surface_fill_mask(foreground_surface, row, col)
            if foreground_mask:
                mask = foreground_mask
                depth_band = _surface_depth_band(
                    foreground_surface,
                    row,
                    col,
                    height * 4,
                )
                color = foreground_depth_codes[depth_band]
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
