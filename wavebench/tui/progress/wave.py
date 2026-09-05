"""Braille wave math — pure rendering functions.

Four kinds of animated braille waves are produced here:

  - ``_title_wave`` — short compact wave for the "Generating" box title.
  - ``_render_pulse_bar`` — token-progress bar that fills as output streams;
    scroll speed is driven by *phase* (accumulated externally) so it tracks
    throughput. Falls back to ``_render_pre_wave_bar`` for empty space.
  - ``_render_pre_wave_bar`` — short reasoning-state wave (1–3 dots high)
    shown before the first completion token arrives.
  - ``render_idle_wave`` — sculpted full-width ocean used on the idle menu
    background; *intensity* (0.0–1.0) controls amplitude, speed, and color.
    A continuous body of water carries a deforming caustic tessellation and
    fine CRT phosphor texture, with distant swells moving at their own pace.

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
    """Change brightness with one shared gain so bright caustics retain their hue."""
    amount = max(0.0, min(amount, 255 / max(max(color), 1)))
    return tuple(round(channel * amount) for channel in color)


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


_IDLE_PHASE_STEP = 0.80
_ACTIVE_PHASE_STEP = 1.70


def _wave_phase_step(intensity: float) -> float:
    """Return the per-frame travel speed for the full-screen ocean.

    The resting wave should still read as moving water, while generated output
    steadily adds momentum. Keeping this interpolation beside the surface math
    ensures the idle menu and the in-progress screen share the same motion
    range.
    """
    intensity = max(0.0, min(1.0, intensity))
    return _IDLE_PHASE_STEP + (_ACTIVE_PHASE_STEP - _IDLE_PHASE_STEP) * intensity


def _crest_harmonics(phase: float, intensity: float) -> float:
    """Narrow each peak and lean its leading face over a broad trough."""
    return -(0.075 + 0.040 * intensity) * math.cos(phase * 2.0) - (
        0.030 + 0.015 * intensity
    ) * math.sin(phase * 2.0)


def _crest_ripple(nx: float, crest_phase: float, phase: float, amp: float, damping: float) -> float:
    """Carry small crossing ripples along a crest at its layer's travel speed."""
    crest = 0.5 + 0.5 * math.sin(crest_phase)
    ripple = 0.65 * math.sin(nx * 57.3 - phase * 0.39 + 1.2) + 0.35 * math.sin(
        nx * 91.7 - phase * 0.61 + 2.8
    )
    return ripple * min(1.2, amp * 0.14) * damping * (0.30 + 0.70 * crest * crest)


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
    # Start with a low, quick swell, then spend most of the available vertical
    # range on generated energy. The linear ramp is deliberately easy to read
    # while ProgressTracker smooths the intensity from frame to frame.
    amp_scale = 0.075 + 0.210 * intensity
    amp_breath = 0.025 + 0.085 * intensity
    amp = total_sp * amp_scale * aspect_damping * (1.0 + amp_breath * math.sin(tick * 0.019 + 1.0))

    center_norm = 0.72 - 0.16 * intensity
    center_sway = 0.045 + 0.130 * intensity
    center = total_sp * center_norm + amp * center_sway * math.sin(tick * 0.024)
    depth_gap = total_sp * (0.075 + 0.025 * intensity)

    if wave_phase is None:
        wave_phase = tick * _wave_phase_step(intensity)

    stokes = 0.018 + 0.080 * intensity
    crest_exp = 1.03 + 0.15 * intensity
    limiter = 0.32 - 0.035 * intensity

    far: list[float] = []
    middle: list[float] = []
    foreground: list[float] = []
    for col in range(width):
        nx = col / max(width - 1, 1)

        far_phase = nx * 12.2 - wave_phase * 0.038 + 0.8
        far_height = (
            0.72 * math.sin(far_phase)
            + _crest_harmonics(far_phase, intensity)
            + (0.065 + 0.025 * intensity) * math.sin(nx * 22.0 - wave_phase * 0.052 + 2.4)
            + (0.010 + 0.020 * intensity) * math.sin(nx * 34.0 - wave_phase * 0.068 + 0.3)
        )
        far_height = _shape_wave(
            far_height,
            stokes * 0.20,
            1.015 + 0.050 * intensity,
            limiter + 0.16,
        )
        far.append(
            center
            - depth_gap * 2.0
            - far_height * amp * 0.34
            - _crest_ripple(nx, far_phase, wave_phase * 0.44 + 12.0, amp * 0.34, aspect_damping)
        )

        middle_phase = nx * 11.2 - wave_phase * 0.061 + 0.3
        middle_height = (
            0.68 * math.sin(middle_phase)
            + _crest_harmonics(middle_phase, intensity)
            + (0.045 + 0.070 * intensity) * math.sin(nx * 20.4 - wave_phase * 0.084 + 2.0)
            + (0.010 + 0.035 * intensity) * math.sin(nx * 32.0 - wave_phase * 0.108 + 3.4)
        )
        middle_height = _shape_wave(
            middle_height,
            stokes * 0.50,
            1.025 + 0.090 * intensity,
            limiter + 0.08,
        )
        middle.append(
            center
            - depth_gap
            - middle_height * amp * 0.62
            - _crest_ripple(nx, middle_phase, wave_phase * 0.71 + 6.0, amp * 0.62, aspect_damping)
        )

        foreground_phase = nx * 10.2 - wave_phase * 0.086
        foreground_height = (
            0.64 * math.sin(foreground_phase)
            + _crest_harmonics(foreground_phase, intensity)
            + (0.050 + 0.080 * intensity) * math.sin(nx * 18.8 - wave_phase * 0.116 + 1.7)
            + (0.012 + 0.040 * intensity) * math.sin(nx * 29.0 - wave_phase * 0.148 + 3.1)
        )
        foreground_height = _shape_wave(
            foreground_height,
            stokes,
            crest_exp,
            limiter,
        )
        foreground.append(
            center
            - foreground_height * amp
            - _crest_ripple(nx, foreground_phase, wave_phase, amp, aspect_damping)
        )

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
    """Return the occupied water dots for one 2×4 braille cell."""
    cell_top = row * 4
    left, right = surface[col * 2], surface[col * 2 + 1]
    # Most cells are wholly above or below the surface. Only the edge needs
    # per-dot rounding, which matters on a large terminal redrawn every frame.
    if left <= cell_top + 0.5 and right <= cell_top + 0.5:
        return 0xFF
    if left > cell_top + 3.5 and right > cell_top + 3.5:
        return 0
    mask = 0
    for subcol in range(2):
        first_wet_dot = math.ceil(surface[col * 2 + subcol] - 0.5) - cell_top
        first_wet_dot = max(0, min(4, first_wet_dot))
        mask |= _BRAILLE_FILL_MASKS[subcol][first_wet_dot]
    return mask


# Depth ramp for the three ocean layers, as a fraction of the active colour.
# Every layer is the same colour scaled down, so depth reads as lightness only.
# Both rear layers stay below the foreground surface highlight, preserving the
# front-to-back depth cue even where deep foreground water is darker.
_FAR_DEPTH = 0.38
_MIDDLE_DEPTH = 0.62
# The foreground gradient combines distance below its local surface with a
# smaller absolute top-to-bottom falloff. Broad color bands keep each frame
# compact for a PTY; the dot geometry supplies the finer texture.
_DEPTH_BANDS = 12
_SURFACE_SHADE = 1.08
_DEEP_SHADE = 0.38
_SCREEN_DEPTH_FALLOFF = 0.16
_SURFACE_RIM_DOTS = 1.0
_BACKGROUND_EFFECT_PADDING = 1.5
# A shared character cannot give its rear dots a separate color. Keep this
# narrow transition at water-body brightness, below the individual crest rim.
_OVERLAP_LIGHTING = 0.80


def _surface_depth_band(
    surface: list[float],
    row: int,
    col: int,
    total_height: int,
    occluding_surface: list[float] | None = None,
    bands: int = _DEPTH_BANDS,
) -> int:
    """Map one water cell to a lightness band below its local surface."""
    surface_y = (surface[col * 2] + surface[col * 2 + 1]) * 0.5
    cell_midpoint = row * 4 + 1.5
    depth = max(0.0, cell_midpoint - surface_y)
    bottom = (
        total_height
        if occluding_surface is None
        else min(total_height, (occluding_surface[col * 2] + occluding_surface[col * 2 + 1]) * 0.5)
    )
    water_column = max(bottom - surface_y, 4.0)
    depth_t = min(1.0, depth / water_column)
    # Smoothstep holds a soft highlight near the surface and avoids crowding
    # most of the available colors into the shallowest cells.
    depth_t = depth_t * depth_t * (3.0 - 2.0 * depth_t)
    return min(bands - 1, round(depth_t * (bands - 1)))


_PhosphorLimits = tuple[tuple[float, ...], ...]
_CausticSite = tuple[float, float, _PhosphorLimits]
_CausticEdge = tuple[float, float, float]
# Keep the light falloff one dot wide, even as the facets grow with the terminal.
_CAUSTIC_FALLOFF_DOTS = 1.0
# Low-contrast shades let refracted light blend into the surrounding water.
_CAUSTIC_LIGHTING = (0.62, 0.67, 0.72, 0.77, 0.82)
_LAYER_LIGHTING = (
    _CAUSTIC_LIGHTING,
    (0.62, 0.70, 0.78),
    (0.64, 0.69, 0.74),
)


def _caustic_grid(
    width: int, height: int, phase: float, layer_index: int = 0
) -> tuple[float, int, list[tuple[_CausticSite, ...]]]:
    """Build an undulating Voronoi tessellation in braille-dot coordinates.

    Light gathers along the boundaries of these irregular cells, like the
    network of caustics on a pool floor. Sites move continuously in two axes;
    their slow deformation is driven by the same accumulated phase as the
    surface. Cache each tile's neighbors once, outside the rendering loop.
    """
    # Distant facets look smaller and catch less light. Lower dot coverage
    # keeps the layers distinct even when a 256-color terminal maps their
    # brightness to the same palette entry. The nearest material is unchanged.
    cell_size = max(8.0, width / 10.0 * (1.0 - 0.14 * layer_index))
    ambient = 0.28 - 0.04 * layer_index
    caustic_gain = 0.40 - 0.08 * layer_index
    columns = math.ceil(width / cell_size) + 1
    rows = math.ceil(height / cell_size) + 1
    time = phase * 0.026
    sites: dict[tuple[int, int], _CausticSite] = {}
    for y in range(-1, rows + 1):
        for x in range(-1, columns + 1):
            seed = x * 127.1 + y * 311.7
            sites[x, y] = (
                x + 0.5 + 0.34 * math.sin(seed + time),
                y + 0.5 + 0.34 * math.sin(seed * 1.37 - time * 0.83),
                _phosphor_limits(
                    ambient + 0.04 * math.sin(seed * 0.7 + time * 0.4),
                    _CAUSTIC_FALLOFF_DOTS / cell_size,
                    caustic_gain,
                ),
            )
    neighbors = [
        tuple(sites[x + dx, y + dy] for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        for y in range(rows)
        for x in range(columns)
    ]
    return 1.0 / cell_size, columns, neighbors


def _caustic_edges(
    sites: tuple[_CausticSite, ...], x: float, y: float, scale: float
) -> tuple[_CausticEdge, _CausticEdge, _PhosphorLimits]:
    """Find the two closest cell boundaries and their per-dot distance steps.

    A boundary is the bisector between two sites. Computing its equation once
    per character lets all eight dots share the expensive neighbor search,
    while still sampling thin highlights at the terminal's full resolution.
    """
    first = second = third = sites[0]
    d1 = d2 = d3 = math.inf
    for site in sites:
        dx, dy = x - site[0], y - site[1]
        distance = dx * dx + dy * dy
        if distance < d1:
            first, second, third = site, first, second
            d1, d2, d3 = distance, d1, d2
        elif distance < d2:
            second, third = site, second
            d2, d3 = distance, d2
        elif distance < d3:
            third, d3 = site, distance

    edges: list[_CausticEdge] = []
    for neighbor, distance in ((second, d2), (third, d3)):
        dx, dy = first[0] - neighbor[0], first[1] - neighbor[1]
        length = math.hypot(dx, dy)
        edges.append((dx * scale / length, dy * scale / length, (distance - d1) / (2 * length)))
    return edges[0], edges[1], first[2]


# Ordered phosphor coverage. The texture stays anchored to the screen, while
# the caustic field moves through it; changing the dither each frame flickers.
_PHOSPHOR = (
    (0.03, 0.53, 0.16, 0.66),
    (0.78, 0.28, 0.91, 0.41),
    (0.22, 0.72, 0.09, 0.59),
    (0.97, 0.47, 0.84, 0.34),
)


def _phosphor_limits(ambient: float, falloff: float, caustic_gain: float = 0.40) -> _PhosphorLimits:
    """Precompute how close each dot must be to a caustic to light up.

    Invert the highlight falloff once per facet instead of evaluating it at
    every dot. The last dot row carries a faint, stationary CRT scanline.
    """
    limits: list[tuple[float, ...]] = []
    for row, thresholds in enumerate(_PHOSPHOR):
        scanline = 0.86 if row == 3 else 1.0
        distances: list[float] = []
        for threshold in thresholds:
            light = (threshold / scanline - ambient) / caustic_gain
            if light < 0:
                distances.append(math.inf)
            else:
                distances.append(falloff * (1.0 - math.sqrt(light)))
        limits.append(tuple(distances))
    return tuple(limits)


def _surface_water_mask(
    surface: list[float],
    edges: tuple[_CausticEdge, _CausticEdge, _PhosphorLimits],
    row: int,
    col: int,
    occluding_surface: list[float] | None = None,
) -> int:
    """Sample a solid surface rim, caustic highlights, and fine CRT scanlines."""
    (ax, ay, a), (bx, by, b), limits = edges
    mask = 0
    for subcol in range(2):
        index = col * 2 + subcol
        surface_y = surface[index]
        near_y = math.inf if occluding_surface is None else occluding_surface[index]
        edge_a = a + ax * (subcol - 0.5) - ay * 1.5
        edge_b = b + bx * (subcol - 0.5) - by * 1.5
        for subrow in range(4):
            depth = row * 4 + subrow + 0.5 - surface_y
            limit = limits[subrow][index % 4]
            lit = abs(edge_a) < limit or abs(edge_b) < limit
            edge_a += ay
            edge_b += by
            if depth < 0 or row * 4 + subrow + 0.5 >= near_y:
                continue
            if depth < _SURFACE_RIM_DOTS:
                mask |= _BRAILLE_DOT_BITS[subrow][subcol]
                continue
            # Preserve quiet water around both silhouettes. Ambient phosphor
            # remains, but focused caustics stay inside the visible layer.
            inset = occluding_surface is None or (
                depth >= _SURFACE_RIM_DOTS + _BACKGROUND_EFFECT_PADDING
                and near_y - (row * 4 + subrow + 0.5) >= _BACKGROUND_EFFECT_PADDING
            )
            if lit and (inset or math.isinf(limit)):
                mask |= _BRAILLE_DOT_BITS[subrow][subcol]
    return mask


def render_idle_wave(
    tick: int, width: int, height: int, intensity: float = 0.0, wave_phase: float | None = None
) -> list[str]:
    """Render rolling water with a caustic tessellation and subtle CRT texture.

    Returns *height* ANSI-colored strings, each *width* visible characters
    wide.  Each surface is sampled at the braille cell's true 2×4 resolution.
    Each layer carries its own moving material. Nearer water hides rear dots
    only below its surface, including gaps in its phosphor texture. Shared
    cells use a quiet blend of the layers' colors, so a front crest cannot
    brighten the rear wave's texture or create a flare where two rims meet.
    """
    if _NO_COLOR or height <= 0 or width <= 0:
        return [" " * width] * max(height, 0)

    intensity = max(0.0, min(1.0, intensity))
    far_surface, middle_surface, foreground_surface = _idle_wave_surfaces(
        tick, width * 2, height, intensity, wave_phase
    )
    phase = tick * _wave_phase_step(intensity) if wave_phase is None else wave_phase
    far_occluding_surface = [
        min(middle_y, foreground_y)
        for middle_y, foreground_y in zip(
            middle_surface,
            foreground_surface,
            strict=True,
        )
    ]

    # Idle water is visible enough to carry the screen; generation lifts it
    # steadily to the theme's brightest shade without changing hue.
    color_t = max(0.0, min(1.0, 0.24 + 0.76 * intensity + 0.02 * math.sin(tick * 0.03)))
    low, middle, high = _styles.IDLE_WAVE_COLORS
    if color_t < 0.5:
        active_color = _blend_color(low, middle, color_t * 2.0)
    else:
        active_color = _blend_color(middle, high, (color_t - 0.5) * 2.0)

    # Distant water lifts slightly as the ocean gains energy, but never past
    # the layer in front of it.
    far_depth = _FAR_DEPTH + 0.06 * intensity
    middle_depth = _MIDDLE_DEPTH + 0.04 * intensity
    # Front to back: surface, occluder, brightness, material phase.
    layers = (
        (foreground_surface, None, 1.0, phase),
        (middle_surface, foreground_surface, middle_depth, phase * 0.71 + 17.0),
        (far_surface, far_occluding_surface, far_depth, phase * 0.44 + 31.0),
    )
    fields = []
    for layer_index, (_surface, _occluder, _depth, material_phase) in enumerate(layers):
        scale, columns, neighbors = _caustic_grid(
            width * 2, height * 4, material_phase, layer_index
        )
        xs = [(col * 2 + 1) * scale for col in range(width)]
        y_offsets = [0.10 * math.sin(x * 3.1 - material_phase * 0.018) for x in xs]
        fields.append((scale, columns, neighbors, xs, y_offsets))

    # Empty sky still has to be cleared, but needs no material or contour work.
    # Skipping its samples keeps large-terminal input responsive.
    top = min(min(far_surface), min(middle_surface), min(foreground_surface))
    first_row = max(0, min(height, math.floor(top) // 4))
    rows: list[str] = [" " * width] * first_row
    for row in range(first_row, height):
        screen_depth = (row + 0.5) / height
        screen_shade = 1.0 - _SCREEN_DEPTH_FALLOFF * screen_depth
        # Only build shades for visible layers and depth bands in this row.
        depth_codes: dict[tuple[int, int], tuple[str, ...]] = {}
        overlap_codes: dict[int, str] = {}
        parts: list[str] = []
        current_color: str | None = None
        caustic_ys = [(row * 4 + 2) * field[0] for field in fields]
        caustic_x_offsets = [
            0.10 * math.sin(y * 3.7 + layer[3] * 0.013)
            for y, layer in zip(caustic_ys, layers, strict=True)
        ]

        for col in range(width):
            mask = covered = 0
            cell_layers = 0
            color_layer = None
            for layer_index, (surface, occluder, layer_depth, _phase) in enumerate(layers):
                fill = _surface_fill_mask(surface, row, col)
                exposed = fill & ~covered
                covered |= fill
                if exposed:
                    # Include occupied water even when its phosphor dot is off.
                    # Otherwise the shared shade would alternate with the dither.
                    cell_layers |= 1 << layer_index
                    scale, columns, caustic_neighbors, xs, y_offsets = fields[layer_index]
                    cx = xs[col] + caustic_x_offsets[layer_index]
                    cy = caustic_ys[layer_index] + y_offsets[col]
                    neighbors = caustic_neighbors[int(cy) * columns + int(cx)]
                    edges = _caustic_edges(neighbors, cx, cy, scale)
                    visible = _surface_water_mask(surface, edges, row, col, occluder) & exposed
                    mask |= visible
                    if visible and color_layer is None:
                        color_layer = layer_index, surface, occluder, layer_depth, edges, scale
                # Stop only once water covers every dot. Stopping at the first
                # partial crest erased the exposed rear water above it, leaving
                # rectangular gaps along overlaps. Texture holes still occlude.
                if covered == 0xFF:
                    break
            if color_layer is None:
                parts.append(" ")
                continue

            layer_index, surface, occluder, layer_depth, edges, scale = color_layer
            # Distant water uses a shorter ramp across its visible depth.
            # Coarser bands keep narrow strips economical to send over a PTY.
            bands = _DEPTH_BANDS if occluder is None else 3
            lighting_levels = _LAYER_LIGHTING[layer_index]
            depth_band = _surface_depth_band(surface, row, col, height * 4, occluder, bands)
            codes = depth_codes.get((layer_index, depth_band))
            if codes is None:
                deep_shade = _DEEP_SHADE if occluder is None else 0.60
                shade = (
                    (_SURFACE_SHADE + (deep_shade - _SURFACE_SHADE) * depth_band / (bands - 1))
                    * screen_shade
                    * layer_depth
                )
                codes = tuple(
                    _color_code(_scale_color(active_color, shade * lighting))
                    for lighting in (*lighting_levels, 0.96)
                )
                depth_codes[layer_index, depth_band] = codes

            surface_y = (surface[col * 2] + surface[col * 2 + 1]) * 0.5
            cell_depth = row * 4 + 2 - surface_y
            rim = cell_depth < _SURFACE_RIM_DOTS
            light = max(
                0.0, 1.0 - min(edges[0][2], edges[1][2]) / (1.5 * _CAUSTIC_FALLOFF_DOTS * scale)
            )
            light = light * light * (3.0 - 2.0 * light)
            if occluder is not None:
                clearance = (occluder[col * 2] + occluder[col * 2 + 1]) * 0.5 - (row * 4 + 2)
                light *= max(
                    0.0,
                    min(
                        1.0,
                        (cell_depth - _SURFACE_RIM_DOTS) / _BACKGROUND_EFFECT_PADDING,
                        clearance / _BACKGROUND_EFFECT_PADDING,
                    ),
                )
            lighting_band = (
                len(lighting_levels) if rim else round(light * (len(lighting_levels) - 1))
            )
            color = codes[lighting_band]
            if cell_layers & (cell_layers - 1):
                # Use quiet, consistent shading across this thin shared edge.
                # Carrying either rim's bright highlight onto all of the other
                # layer's dots made intersections flash as dense, blocky patches.
                color = overlap_codes.get(cell_layers)
                if color is None:
                    depth = (
                        sum(
                            layer[2]
                            for index, layer in enumerate(layers)
                            if cell_layers & (1 << index)
                        )
                        / cell_layers.bit_count()
                    )
                    shade = _SURFACE_SHADE * screen_shade * depth * _OVERLAP_LIGHTING
                    color = _color_code(_scale_color(active_color, shade))
                    overlap_codes[cell_layers] = color
            # Keep color runs open across blank texture to avoid excess SGR.
            if color != current_color:
                parts.append(color)
                current_color = color
            parts.append(chr(0x2800 + mask))

        if current_color is not None:
            parts.append(S.RST)
        rows.append("".join(parts))

    return rows
