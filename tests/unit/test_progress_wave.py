"""Visual geometry, terminal compatibility, and motion of the wave renderers."""

from __future__ import annotations

import re
from itertools import pairwise
from statistics import fmean, median
from types import SimpleNamespace

import pytest

from wavebench.tui import styles
from wavebench.tui.progress import wave as wave_mod

_COLOR_OR_RESET = re.compile(r"\033\[(?:38;2;(\d+);(\d+);(\d+)|0)m")

# xterm-256 palette, for decoding the reduced-color path back to RGB.
_PALETTE = {
    16 + 36 * r + 6 * g + b: (
        styles._CUBE_LEVELS[r],
        styles._CUBE_LEVELS[g],
        styles._CUBE_LEVELS[b],
    )
    for r in range(6)
    for g in range(6)
    for b in range(6)
} | {232 + n: (8 + 10 * n,) * 3 for n in range(24)}

_ANY_COLOR = re.compile(r"\033\[38;(?:2;(\d+);(\d+);(\d+)|5;(\d+))m")


@pytest.fixture(autouse=True)
def truecolor(monkeypatch):
    """Pin 24-bit output. Otherwise these tests read whatever the terminal
    running them happens to support — inside tmux that is the palette path."""
    monkeypatch.setattr(styles, "TRUECOLOR", True)


@pytest.fixture
def palette(monkeypatch):
    """Opposite of :func:`truecolor` — force the reduced 256-color path."""
    monkeypatch.setattr(styles, "TRUECOLOR", False)


@pytest.fixture
def colored(monkeypatch):
    """Force color on — the renderers no-op into plain text under NO_COLOR."""
    monkeypatch.setattr(wave_mod, "_NO_COLOR", False)
    monkeypatch.setattr(wave_mod.S, "RST", "\033[0m")


_ANY_COLOR_OR_RESET = re.compile(r"\033\[(?:38;2;(\d+);(\d+);(\d+)|38;5;(\d+)|0)m")


def _rendered_colors(rendered: str) -> list[tuple[int, int, int]]:
    """Every color in a rendered wave, whichever escape form carries it."""
    return [
        _PALETTE[int(match.group(4))] if match.group(4) else tuple(map(int, match.group(1, 2, 3)))
        for match in _ANY_COLOR.finditer(rendered)
    ]


def _lit_cell_colors(rendered: str) -> list[tuple[int, int, int]]:
    """Color of each *lit* cell, with color runs expanded and blanks dropped.

    Counting escapes instead would weight the dithered path differently from
    the 24-bit one: escapes are only emitted where the color changes, and
    dithering changes it far more often than a flat run does.
    """
    cells: list[tuple[str, tuple[int, int, int] | None]] = []
    current: tuple[int, int, int] | None = None
    pos = 0
    for match in _ANY_COLOR_OR_RESET.finditer(rendered):
        cells.extend((char, current) for char in rendered[pos : match.start()])
        if match.group(4) is not None:
            current = _PALETTE[int(match.group(4))]
        elif match.group(1) is not None:
            current = tuple(map(int, match.group(1, 2, 3)))
        else:
            current = None
        pos = match.end()
    cells.extend((char, current) for char in rendered[pos:])
    return [color for char, color in cells if char != " " and color is not None]


def _cell_colors(rendered: str) -> list[tuple[int, int, int]]:
    """Per-character RGB of a rendered wave, with ANSI runs expanded out."""
    colors: list[tuple[int, int, int]] = []
    current = (0, 0, 0)
    pos = 0
    for match in _COLOR_OR_RESET.finditer(rendered):
        colors.extend(current for _ in rendered[pos : match.start()])
        current = (0, 0, 0) if match.group(1) is None else tuple(map(int, match.groups()))
        pos = match.end()
    colors.extend(current for _ in rendered[pos:])
    return colors


def _hue(color: tuple[int, int, int]) -> tuple[float, ...]:
    """Channel ratios against the peak channel — brightness-independent."""
    peak = max(color) or 1
    return tuple(channel / peak for channel in color)


def _hue_drift(colors: list[tuple[int, int, int]]) -> float:
    """Widest per-channel spread across a set of colors. 0.0 is one exact hue."""
    hues = [_hue(color) for color in colors if sum(color) > 25]
    return max(max(hue[i] for hue in hues) - min(hue[i] for hue in hues) for i in range(3))


_GLYPH_LEVEL = {
    glyph: level for level, glyphs in enumerate(wave_mod._WAVE_CHARS) for glyph in glyphs
}


def _wave_levels(rendered: str) -> list[int]:
    visible = re.sub(r"\033\[[0-9;]*m", "", rendered)
    return [_GLYPH_LEVEL[glyph] for glyph in visible]


def test_idle_wave_layers_are_ordered_back_to_front() -> None:
    layers = wave_mod._idle_wave_surfaces(
        tick=24,
        width=80,
        height=16,
        intensity=0.7,
        wave_phase=18.0,
    )

    assert len(layers) == 3
    assert all(len(layer) == 80 for layer in layers)
    far, middle, foreground = layers
    assert fmean(far) < fmean(middle) < fmean(foreground)


def test_idle_wave_layers_move_with_parallax() -> None:
    first = wave_mod._idle_wave_surfaces(
        tick=24,
        width=100,
        height=18,
        intensity=0.8,
        wave_phase=10.0,
    )
    second = wave_mod._idle_wave_surfaces(
        tick=24,
        width=100,
        height=18,
        intensity=0.8,
        wave_phase=18.0,
    )

    motion = [
        fmean(abs(after - before) for before, after in zip(old, new, strict=True))
        for old, new in zip(first, second, strict=True)
    ]
    assert motion[0] < motion[1] < motion[2]


def test_idle_wave_starts_low_and_builds_height_and_motion_gradually() -> None:
    """Resting water stays low but visibly moves, then gains energy evenly."""
    width, height = 180, 18
    intensities = (0.0, 0.25, 0.5, 0.75, 1.0)
    spans: list[float] = []
    top_rows: list[float] = []
    motions: list[float] = []

    for intensity in intensities:
        sampled_spans: list[float] = []
        sampled_tops: list[float] = []
        sampled_motion: list[float] = []
        phase_step = wave_mod._wave_phase_step(intensity)

        for tick in (0, 30, 60, 90):
            for phase in (0.0, 24.0, 48.0):
                layers = wave_mod._idle_wave_surfaces(
                    tick,
                    width,
                    height,
                    intensity,
                    wave_phase=phase,
                )
                foreground = layers[-1]
                sampled_spans.append((max(foreground) - min(foreground)) / 4.0)
                sampled_tops.append(min(min(surface) for surface in layers) / 4.0)

                next_layers = wave_mod._idle_wave_surfaces(
                    tick + 1,
                    width,
                    height,
                    intensity,
                    wave_phase=phase + phase_step,
                )
                sampled_motion.append(
                    fmean(
                        abs(after - before)
                        for old, new in zip(layers, next_layers, strict=True)
                        for before, after in zip(old, new, strict=True)
                    )
                )

        spans.append(fmean(sampled_spans))
        top_rows.append(fmean(sampled_tops))
        motions.append(fmean(sampled_motion))

    span_steps = [after - before for before, after in pairwise(spans)]
    motion_steps = [after - before for before, after in pairwise(motions)]

    assert spans == sorted(spans)
    assert motions == sorted(motions)
    assert top_rows == sorted(top_rows, reverse=True)
    assert spans[0] < height * 0.10
    # Small crest ripples add height at rest; the swell still grows roughly fourfold.
    assert spans[-1] > spans[0] * 3.8
    assert motions[0] > 0.07
    assert motions[-1] > motions[0] * 8.0
    assert max(span_steps) < min(span_steps) * 1.25
    assert max(motion_steps) < sum(motion_steps) * 0.40


@pytest.mark.parametrize(("width", "height"), [(78, 14), (118, 20), (78, 40)])
def test_idle_wave_surfaces_keep_a_gentle_slope(width: int, height: int) -> None:
    """Crested swells keep their leading faces below a row per column.

    Surface samples are half a terminal column apart and use four vertical
    dots per terminal row, so ``delta / 2`` is the rise in rows per column.
    The tall/narrow case also guards the aspect-ratio amplitude damping.
    """
    for tick in (0, 40, 120):
        for phase in (0.0, 26.0, 90.0):
            for surface in wave_mod._idle_wave_surfaces(
                tick=tick,
                width=width * 2,
                height=height,
                intensity=1.0,
                wave_phase=phase,
            ):
                slopes = [abs(after - before) / 2.0 for before, after in pairwise(surface)]
                assert max(slopes) <= 0.80


def test_compact_waves_change_height_gradually() -> None:
    for tick in range(0, 80, 5):
        title = _wave_levels(wave_mod._title_wave(tick, width=20))
        reasoning = _wave_levels(wave_mod._render_pre_wave_bar(40, tick))

        assert max(abs(after - before) for before, after in pairwise(title)) <= 2
        assert max(abs(after - before) for before, after in pairwise(reasoning)) <= 1


def test_progress_wave_has_no_sheer_edge() -> None:
    for tick in (0, 30, 75):
        for phase in (0.0, 2.0, 5.5):
            for chars in (100, 250, 500, 750, 900, 1000):
                levels = _wave_levels(
                    wave_mod._render_pulse_bar(
                        chars,
                        1000,
                        tick,
                        phase=phase,
                        bar_width=28,
                    )
                )
                assert max(abs(after - before) for before, after in pairwise(levels)) <= 3


def test_tracker_wave_volume_scales_to_each_run() -> None:
    from wavebench.tui.progress import ProgressTracker

    tracker = ProgressTracker(
        total=2,
        results={},
        model_names=["small", "large"],
        avg_tokens={"small": 500, "large": 1500},
    )

    assert tracker._wave_volume_factor(0) == 0.0
    assert tracker._wave_volume_factor(2_000) == pytest.approx(0.5)
    assert tracker._wave_volume_factor(8_000) == 1.0


def test_surface_fill_mask_samples_both_braille_columns() -> None:
    mask = wave_mod._surface_fill_mask([0.2, 2.8], row=0, col=0)

    assert mask == 0xC7


def test_foreground_gradient_tracks_depth_below_local_surface(monkeypatch) -> None:
    monkeypatch.setattr(wave_mod, "_NO_COLOR", False)
    monkeypatch.setattr(wave_mod.S, "RST", "\033[0m")
    monkeypatch.setattr(
        wave_mod._styles,
        "IDLE_WAVE_COLORS",
        ((10, 20, 30), (100, 120, 140), (240, 250, 255)),
    )
    monkeypatch.setattr(
        wave_mod,
        "_idle_wave_surfaces",
        lambda *_args, **_kwargs: (
            [9.0] * 4,
            [9.0] * 4,
            [2.4, 2.4, -20.0, -20.0],
        ),
    )

    row = wave_mod.render_idle_wave(tick=0, width=2, height=1, intensity=0.5)[0]
    visible = re.sub(r"\033\[[0-9;]*m", "", row)
    colors = _cell_colors(row)

    assert visible[0] == "⠤"
    # Deep water has fine phosphor texture instead of a solid block.
    assert 0 < (ord(visible[1]) - 0x2800).bit_count() < 8
    assert len(colors) == 2
    assert sum(colors[0]) > 2 * sum(colors[1])


def test_foreground_gradient_also_darkens_lower_cells(monkeypatch) -> None:
    monkeypatch.setattr(wave_mod, "_NO_COLOR", False)
    monkeypatch.setattr(wave_mod.S, "RST", "\033[0m")
    monkeypatch.setattr(
        wave_mod._styles,
        "IDLE_WAVE_COLORS",
        ((10, 20, 30), (100, 120, 140), (240, 250, 255)),
    )
    monkeypatch.setattr(
        wave_mod,
        "_idle_wave_surfaces",
        lambda *_args, **_kwargs: (
            [20.0] * 4,
            [20.0] * 4,
            [2.4, 2.4, 6.4, 6.4],
        ),
    )

    rows = wave_mod.render_idle_wave(tick=0, width=2, height=2, intensity=0.5)
    upper_colors = _cell_colors(rows[0])
    lower_colors = _cell_colors(rows[1])

    # Both are surface-band cells, but the lower one receives screen-depth
    # attenuation. Within that lower row, submerged water remains darker still.
    assert sum(upper_colors[0]) > sum(lower_colors[1]) > sum(lower_colors[0])


def test_background_water_does_not_leak_across_foreground_edge(monkeypatch) -> None:
    monkeypatch.setattr(wave_mod, "_NO_COLOR", False)
    monkeypatch.setattr(wave_mod.S, "RST", "\033[0m")
    monkeypatch.setattr(
        wave_mod,
        "_idle_wave_surfaces",
        lambda *_args, **_kwargs: (
            [9.0, 9.0],
            [3.9, 3.9],
            [3.8, 3.8],
        ),
    )

    row = wave_mod.render_idle_wave(tick=0, width=1, height=1, intensity=0.5)[0]
    visible = re.sub(r"\033\[[0-9;]*m", "", row)

    assert visible == " "


def test_far_water_is_occluded_below_middle_surface(monkeypatch) -> None:
    monkeypatch.setattr(wave_mod, "_NO_COLOR", False)
    monkeypatch.setattr(wave_mod.S, "RST", "\033[0m")
    monkeypatch.setattr(
        wave_mod,
        "_idle_wave_surfaces",
        lambda *_args, **_kwargs: (
            [5.2, 5.2],
            [2.2, 2.2],
            [9.0, 9.0],
        ),
    )

    rows = wave_mod.render_idle_wave(tick=0, width=1, height=2, intensity=0.5)
    visible = [re.sub(r"\033\[[0-9;]*m", "", row) for row in rows]

    assert visible[0] == "⠤"
    assert visible[1] != " ", "the middle wave should have water beneath its crest"
    monkeypatch.setattr(
        wave_mod,
        "_idle_wave_surfaces",
        lambda *_args, **_kwargs: ([100.0, 100.0], [2.2, 2.2], [9.0, 9.0]),
    )
    assert rows == wave_mod.render_idle_wave(tick=0, width=1, height=2, intensity=0.5)


def test_render_idle_wave_renders_distinct_water_layers(monkeypatch) -> None:
    monkeypatch.setattr(wave_mod, "_NO_COLOR", False)
    monkeypatch.setattr(wave_mod.S, "RST", "\033[0m")

    def surfaces(*_args, **_kwargs):
        return (
            [0.2] * 6,
            [9.0, 9.0, 1.2, 1.2, 1.2, 1.2],
            [9.0, 9.0, 9.0, 9.0, 2.4, 2.4],
        )

    monkeypatch.setattr(wave_mod, "_idle_wave_surfaces", surfaces)

    row = wave_mod.render_idle_wave(tick=0, width=3, height=1, intensity=0.5)[0]
    visible = re.sub(r"\033\[[0-9;]*m", "", row)
    colors = re.findall(r"\033\[38;2;(\d+);(\d+);(\d+)m", row)

    assert len(visible) == 3
    for glyph, rim in zip(visible, (0x09, 0x12, 0x24), strict=True):
        assert (ord(glyph) - 0x2800) & rim == rim
    assert len(set(colors)) == 3


def test_render_idle_wave_uses_multiple_depth_colors(monkeypatch) -> None:
    monkeypatch.setattr(wave_mod, "_NO_COLOR", False)
    monkeypatch.setattr(wave_mod.S, "RST", "\033[0m")
    monkeypatch.setattr(
        wave_mod._styles,
        "IDLE_WAVE_COLORS",
        ((10, 45, 80), (25, 125, 190), (90, 220, 255)),
    )

    rows = wave_mod.render_idle_wave(
        tick=30,
        width=72,
        height=14,
        intensity=0.75,
        wave_phase=20.0,
    )

    colors = {
        tuple(map(int, match))
        for row in rows
        for match in re.findall(r"\033\[38;2;(\d+);(\d+);(\d+)m", row)
    }
    assert len(colors) >= 4
    assert all(styles._vlen(row) == 72 for row in rows)


@pytest.mark.parametrize(("width", "height"), [(38, 9), (78, 14), (118, 26)])
def test_water_has_a_continuous_crest_and_a_connected_body(
    colored, width: int, height: int
) -> None:
    """Caustics leave fine texture, but never cut empty bands through the water."""
    for intensity in (0.0, 0.5, 1.0):
        for tick in (0, 40, 100):
            surfaces = wave_mod._idle_wave_surfaces(tick, width * 2, height, intensity, None)
            foreground = surfaces[-1]
            skyline = [min(ys) for ys in zip(*surfaces, strict=True)]
            rows = wave_mod.render_idle_wave(tick, width, height, intensity)
            water_dots = lit_dots = 0
            for row_index, rendered in enumerate(rows):
                visible = re.sub(r"\033\[[0-9;]*m", "", rendered)
                for col, glyph in enumerate(visible):
                    water = wave_mod._surface_fill_mask(foreground, row_index, col)
                    if not water:
                        continue
                    mask = ord(glyph) - 0x2800 if glyph != " " else 0
                    water_dots += water.bit_count()
                    lit_dots += (mask & water).bit_count()
                    assert mask & ~wave_mod._surface_fill_mask(skyline, row_index, col) == 0
                    if water == 0xFF:
                        assert mask, "submerged cells must retain some water between highlights"
                    for subcol in range(2):
                        for subrow in range(4):
                            depth = row_index * 4 + subrow + 0.5 - foreground[col * 2 + subcol]
                            if 0 <= depth < 1.0:
                                assert mask & wave_mod._BRAILLE_DOT_BITS[subrow][subcol]

            assert 0.28 < lit_dots / water_dots < 0.60


@pytest.mark.parametrize("near_layer", (1, 2))
def test_distant_water_cannot_shine_through_nearer_texture(
    colored, monkeypatch, near_layer
) -> None:
    """Neither foreground nor middle-water texture gaps expose farther water."""
    surfaces = [[7.2] * 12, [100.0] * 12, [100.0] * 12]
    surfaces[near_layer] = [0.2] * 12
    monkeypatch.setattr(
        wave_mod,
        "_idle_wave_surfaces",
        lambda *_args: tuple(surfaces),
    )
    with_background = wave_mod.render_idle_wave(40, 6, 6, 0.5)
    surfaces[0] = [100.0] * 12
    without_background = wave_mod.render_idle_wave(40, 6, 6, 0.5)

    assert with_background == without_background


@pytest.mark.parametrize(("rear_layer", "near_layer"), [(0, 1), (0, 2), (1, 2)])
@pytest.mark.parametrize("crest", [(1.2, 1.2), (2.2, 2.2), (3.2, 3.2), (1.2, 3.2), (3.2, 1.2)])
def test_contour_cells_never_contain_rear_wave_dots(
    colored, monkeypatch, rear_layer, near_layer, crest
) -> None:
    """A rear rim cannot protrude above a foreground crest inside its character."""
    surfaces = [[100.0] * 4 for _ in range(3)]
    surfaces[near_layer] = list(crest) * 2
    monkeypatch.setattr(wave_mod, "_idle_wave_surfaces", lambda *_args: tuple(surfaces))
    without_rear = wave_mod.render_idle_wave(40, 2, 2, 0.7)
    surfaces[rear_layer] = [-4.0] * 4
    with_rear = wave_mod.render_idle_wave(40, 2, 2, 0.7)

    # Both glyph geometry and color must remain the nearer wave's own. A
    # brightness fade can hide the problem but cannot satisfy this comparison.
    assert with_rear == without_rear


def test_rear_water_cutout_follows_the_contour_in_dot_steps(colored, monkeypatch) -> None:
    """The clearance curves with the crest instead of erasing rectangular cells."""
    foreground = [9.2, 9.2, 10.2, 10.2, 11.2, 11.2]
    monkeypatch.setattr(
        wave_mod,
        "_idle_wave_surfaces",
        lambda *_args: ([100.0] * 6, [0.2] * 6, foreground),
    )
    # Fully lit water reveals the clipping boundary without texture hiding it.
    monkeypatch.setattr(
        wave_mod,
        "_caustic_edges",
        lambda *_args: ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), ((float("inf"),) * 4,) * 4),
    )
    rows = wave_mod.render_idle_wave(40, 3, 4, 0.7)
    visible = [re.sub(r"\033\[[0-9;]*m", "", row) for row in rows]

    assert [ord(char) - 0x2800 for char in visible[1]] == [0x1B, 0x3F, 0xFF]
    for col, char in enumerate(visible[2]):
        assert ord(char) - 0x2800 == wave_mod._surface_fill_mask(foreground, 2, col)


@pytest.mark.parametrize(("width", "height"), [(38, 9), (78, 14), (118, 26), (300, 60)])
def test_animated_wave_layers_never_light_the_same_character(
    colored, monkeypatch, width, height
) -> None:
    """Moving crossings cannot combine two silhouettes, at any tested terminal size."""
    sample = wave_mod._surface_water_mask
    occupied = set()

    def record(surface, edges, row, col, occluder=None):
        mask = sample(surface, edges, row, col, occluder)
        if mask:
            assert (row, col) not in occupied, "two wave silhouettes share one character"
            occupied.add((row, col))
        return mask

    monkeypatch.setattr(wave_mod, "_surface_water_mask", record)
    for intensity in (0.0, 0.5, 1.0):
        for phase in (0.0, 26.0, 90.0):
            occupied.clear()
            wave_mod.render_idle_wave(40, width, height, intensity, phase)
            assert occupied


@pytest.mark.parametrize(("width", "height"), [(1, 1), (10, 3), (40, 60), (300, 60)])
def test_main_wave_fits_small_and_resized_terminals(colored, width: int, height: int) -> None:
    for intensity in (0.0, 1.0):
        rows = wave_mod.render_idle_wave(40, width, height, intensity)
        assert len(rows) == height
        for row in rows:
            visible = re.sub(r"\033\[[0-9;]*m", "", row)
            assert len(visible) == width
            assert all(glyph == " " or "\u2801" <= glyph <= "\u28ff" for glyph in visible)
            assert "\033[" not in row or row.endswith("\033[0m")


@pytest.mark.parametrize("layer", (0, 1, 2))
def test_caustics_light_local_patches_instead_of_horizontal_bands(
    colored, monkeypatch, layer
) -> None:
    """Even flat water has local shimmer, blended gently into its base color."""
    width, height = 100, 18
    surfaces = [[100.0] * (width * 2) for _ in range(3)]
    surfaces[layer] = [4.0] * (width * 2)
    monkeypatch.setattr(
        wave_mod,
        "_idle_wave_surfaces",
        lambda *_args: tuple(surfaces),
    )
    for phase in (0.0, 30.0, 75.0):
        rows = wave_mod.render_idle_wave(40, width, height, 0.8, wave_phase=phase)
        for row in rows[3:]:
            brightness = [sum(color) for color in _cell_colors(row)]
            assert 1.1 < max(brightness) / min(brightness) < 1.5
            assert len(set(brightness)) >= 3, "caustic light should fade through softer shades"
            transitions = sum(a != b for a, b in pairwise(brightness))
            assert transitions >= 4, "light should gather around separate caustic cells"


@pytest.mark.parametrize("layer", (0, 1, 2))
def test_caustics_deform_smoothly_with_the_accumulated_phase(colored, monkeypatch, layer) -> None:
    """The texture flows independently of the silhouette, without random frame noise."""
    width, height = 100, 18
    surfaces = [[100.0] * (width * 2) for _ in range(3)]
    surfaces[layer] = [4.0] * (width * 2)
    monkeypatch.setattr(
        wave_mod,
        "_idle_wave_surfaces",
        lambda *_args: tuple(surfaces),
    )
    frames = []
    for phase in (30.0, 31.0, 60.0):
        rows = wave_mod.render_idle_wave(40, width, height, 0.8, wave_phase=phase)
        frames.append(re.sub(r"\033\[[0-9;]*m", "", "".join(rows)))
    changes = [
        sum((ord(a) ^ ord(b)).bit_count() for a, b in zip(frames[0], frame, strict=True))
        for frame in frames[1:]
    ]
    assert 0 < changes[0] < width * height * 8 * 0.06
    assert changes[1] > changes[0] * 3


def test_background_caustics_leave_a_quiet_inset_at_both_edges(colored, monkeypatch) -> None:
    width, height = 12, 8
    monkeypatch.setattr(
        wave_mod,
        "_idle_wave_surfaces",
        lambda *_args: ([100.0] * 24, [4.0] * 24, [20.0] * 24),
    )
    frames = []
    for distance in (100.0, 0.0):
        monkeypatch.setattr(
            wave_mod,
            "_caustic_edges",
            lambda *_args, d=distance: (
                (0.0, 0.0, d),
                (0.0, 0.0, d),
                wave_mod._phosphor_limits(0.28, 1.0),
            ),
        )
        frames.append(
            [
                re.sub(r"\033\[[0-9;]*m", "", row)
                for row in wave_mod.render_idle_wave(40, width, height, 0.8)
            ]
        )

    changed = 0
    for row in range(5):  # The foreground owns every row from its surface at y=20.
        for before, after in zip(frames[0][row], frames[1][row], strict=True):
            difference = ord(before) ^ ord(after)
            for subrow, bits in enumerate(wave_mod._BRAILLE_DOT_BITS):
                if difference & (bits[0] | bits[1]):
                    y = row * 4 + subrow + 0.5
                    assert 6.5 <= y <= 18.5, "focused light must stay away from both rims"
                    changed += 1
    assert changed, "the inset should still leave room for caustics inside the layer"


@pytest.mark.parametrize("layer", (0, 1, 2))
def test_crest_highlight_fades_gently_into_quiet_water(colored, monkeypatch, layer) -> None:
    """Moving down from a crest gives a small gradient, without a bright cutoff."""
    depths = (-1.0, 0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0)
    surfaces = [[100.0] * 2 for _ in range(3)]
    # Move the crest past one character. The screen gradient and depth band
    # stay constant, isolating its lighting as it crosses character rows.
    monkeypatch.setattr(wave_mod, "_idle_wave_surfaces", lambda *_args: tuple(surfaces))
    monkeypatch.setattr(
        wave_mod,
        "_caustic_edges",
        lambda *_args: ((0.0, 0.0, 100.0), (0.0, 0.0, 100.0), ((float("inf"),) * 4,) * 4),
    )
    styles.apply_theme("acai")
    try:
        brightness = []
        for depth in depths:
            surfaces[layer] = [18.0 - depth] * 2
            row = wave_mod.render_idle_wave(40, 1, 22, 1.0, 26.0)[4]
            brightness.append(sum(_cell_colors(row)[0]))
        assert brightness == sorted(brightness, reverse=True)
        assert len(set(brightness)) >= 4, "crest light needs intermediate shades"
        assert brightness[0] <= brightness[-1] * 1.15, "the crest should be barely brighter"
        span = brightness[0] - brightness[-1]
        assert max(a - b for a, b in pairwise(brightness)) <= span * 0.4
    finally:
        styles.apply_theme("default")


# ── Hue lock ──────────────────────────────────────────────────────────────
#
# Every wave is one theme color varied in lightness. Regression guards for the
# two ways that broke before: a palette whose endpoints were not the same hue,
# and pulsing brightness by adding equal amounts to R/G/B (which desaturates
# toward grey instead of dimming).


def test_bar_waves_stay_on_a_single_hue(colored) -> None:
    sampled: list[tuple[int, int, int]] = []
    for chars in (0, 100, 250, 500, 900, 1000):
        sampled += _cell_colors(wave_mod._render_pulse_bar(chars, 1000, 30, phase=2.0))
    sampled += _cell_colors(wave_mod._render_pre_wave_bar(20, 30))

    assert _hue_drift(sampled) < 0.08


@pytest.mark.parametrize("theme", styles.THEME_NAMES)
def test_idle_wave_stays_on_a_single_hue(colored, theme: str) -> None:
    styles.apply_theme(theme)
    sampled: list[tuple[int, int, int]] = []
    for intensity in (0.0, 0.5, 1.0):
        for row in wave_mod.render_idle_wave(
            tick=40, width=90, height=16, intensity=intensity, wave_phase=26.0
        ):
            sampled += [
                tuple(map(int, match))
                for match in re.findall(r"\033\[38;2;(\d+);(\d+);(\d+)m", row)
            ]

    try:
        assert _hue_drift(sampled) < 0.08
    finally:
        styles.apply_theme("default")


def test_every_theme_keeps_its_bar_wave_on_one_hue(colored) -> None:
    for name in styles.THEME_NAMES:
        styles.apply_theme(name)
        sampled = _cell_colors(wave_mod._render_pulse_bar(300, 1000, 30, phase=2.0))
        sampled += _cell_colors(wave_mod._render_pre_wave_bar(20, 30))
        assert _hue_drift(sampled) < 0.08, name
    styles.apply_theme("default")


# ── Gradient continuity ───────────────────────────────────────────────────


def test_pulse_bar_has_no_color_seam_where_the_fill_ends(colored) -> None:
    """The generated section and the trailing pre-wave are one gradient.

    They used to be two palettes butted together, so brightness jumped by an
    order of magnitude more than a normal step at the boundary.
    """
    bar_width = 28
    for chars in (100, 250, 500, 750):
        colors = _cell_colors(
            wave_mod._render_pulse_bar(chars, 1000, 30, phase=2.0, bar_width=bar_width)
        )
        assert len(colors) == bar_width

        brightness = [sum(color) for color in colors]
        steps = [abs(b - a) for a, b in pairwise(brightness)]
        seam = steps[round(chars / 1000 * bar_width) - 1]

        assert seam <= 2 * median(steps), f"{chars=} seam={seam} steps={steps}"


def test_pulse_bar_brightness_peaks_at_the_leading_edge(colored) -> None:
    bar_width = 28
    for chars in (250, 500, 750, 1000):
        colors = _cell_colors(
            wave_mod._render_pulse_bar(chars, 1000, 30, phase=2.0, bar_width=bar_width)
        )
        brightness = [sum(color) for color in colors]
        crest = round(chars / 1000 * bar_width) - 1

        assert abs(brightness.index(max(brightness)) - crest) <= 1


def test_bar_color_tracks_position_not_wave_height(colored) -> None:
    """Height belongs to the glyph. Keying color off the quantised height level
    banded neighbouring cells instead of blending them."""
    colors = _cell_colors(wave_mod._render_pulse_bar(1000, 1000, 30, phase=2.0, bar_width=28))
    brightness = [sum(color) for color in colors]

    # A full bar ramps monotonically up to its leading edge, whatever shape the
    # wave happens to have underneath it.
    assert brightness == sorted(brightness)


# ── Depth ordering ────────────────────────────────────────────────────────


def test_idle_wave_depth_colors_are_ordered(colored) -> None:
    """Surface, distance, and underwater lightness retain clear depth cues."""
    tick, width, height, phase = 40, 150, 20, 26.0

    for intensity in (0.0, 0.5, 1.0):
        far, middle, foreground = wave_mod._idle_wave_surfaces(
            tick, width * 2, height, intensity, phase
        )
        rows = wave_mod.render_idle_wave(tick, width, height, intensity, wave_phase=phase)

        surface_bodies: list[int] = []
        deep_bodies: list[int] = []
        middle_rims: list[int] = []
        far_rims: list[int] = []
        for row_index, row in enumerate(rows):
            colors = _cell_colors(row)
            glyphs = re.sub(r"\033\[[0-9;]*m", "", row)
            assert len(colors) == width
            for col, color in enumerate(colors):
                if glyphs[col] == " ":
                    continue
                if wave_mod._surface_fill_mask(foreground, row_index, col):
                    band = wave_mod._surface_depth_band(
                        foreground,
                        row_index,
                        col,
                        height * 4,
                    )
                    surface_y = (foreground[col * 2] + foreground[col * 2 + 1]) * 0.5
                    if row_index * 4 + 2 - surface_y < 1.0:
                        surface_bodies.append(sum(color))
                    elif band == wave_mod._DEPTH_BANDS - 1:
                        deep_bodies.append(sum(color))
                elif wave_mod._surface_fill_mask(middle, row_index, col):
                    surface_y = (middle[col * 2] + middle[col * 2 + 1]) * 0.5
                    if row_index * 4 + 2 - surface_y < 1.0:
                        middle_rims.append(sum(color))
                elif wave_mod._surface_fill_mask(far, row_index, col):
                    surface_y = (far[col * 2] + far[col * 2 + 1]) * 0.5
                    if row_index * 4 + 2 - surface_y < 1.0:
                        far_rims.append(sum(color))

        assert surface_bodies and deep_bodies and middle_rims and far_rims, intensity
        assert max(far_rims) < min(middle_rims), intensity
        assert max(middle_rims) < min(surface_bodies), intensity
        assert max(deep_bodies) < min(surface_bodies), intensity


# ── Reduced-color terminals ───────────────────────────────────────────────
#
# tmux without RGB passthrough re-quantises 24-bit output to the 256-color
# palette, choosing between the nearest color-cube entry and the nearest
# *greyscale* entry. The cube has no dark saturated colors, so a dim theme
# color lands on grey and one step of lightness flips a cell from colored to
# colorless — the wave banded green/grey and the band swept as it animated.


def _achromatic(color: tuple[int, int, int]) -> bool:
    return sum(color) > 0 and color[0] == color[1] == color[2]


def test_reduced_color_waves_never_render_grey(colored, palette) -> None:
    for name in styles.THEME_NAMES:
        styles.apply_theme(name)

        rendered = [
            wave_mod._render_pulse_bar(chars, 1000, 30, phase=2.0)
            for chars in (0, 100, 300, 600, 1000)
        ]
        rendered.append(wave_mod._render_pre_wave_bar(20, 30))
        for intensity in (0.0, 0.5, 1.0):
            rendered += wave_mod.render_idle_wave(
                tick=40, width=90, height=18, intensity=intensity, wave_phase=26.0
            )

        grey = [c for text in rendered for c in _rendered_colors(text) if _achromatic(c)]
        assert not grey, f"{name}: {sorted(set(grey))}"
    styles.apply_theme("default")


def test_reduced_color_acai_idle_crest_does_not_flash_cyan(colored, palette) -> None:
    """Idle Acai's crest must not switch hue as it moves through character rows."""
    styles.apply_theme("acai")
    try:
        for tick, phase in ((0, 0.0), (40, 26.0), (120, 90.0)):
            rows = wave_mod.render_idle_wave(tick, 150, 22, 0.0, wave_phase=phase)
            colors = [color for row in rows for color in _lit_cell_colors(row)]
            assert colors
            assert _hue_drift(colors) < 0.08, (tick, set(colors))
    finally:
        styles.apply_theme("default")


@pytest.mark.parametrize("theme", styles.THEME_NAMES)
def test_reduced_color_crests_keep_soft_lighting(colored, palette, monkeypatch, theme) -> None:
    """A small rim highlight cannot make water several times brighter."""
    # Alternate a rim and shallow water within one row, at the same depth band.
    # This reproduces the broken bright segments as a crest crosses cell rows.
    monkeypatch.setattr(
        wave_mod,
        "_idle_wave_surfaces",
        lambda *_args: ([100.0] * 20, [100.0] * 20, [7.2, 7.2, 9.2, 9.2] * 5),
    )
    monkeypatch.setattr(
        wave_mod,
        "_caustic_edges",
        lambda *_args: (
            (0.0, 0.0, 100.0),
            (0.0, 0.0, 100.0),
            wave_mod._phosphor_limits(0.28, 1.0),
        ),
    )
    styles.apply_theme(theme)
    try:
        for intensity in (0.0, 0.25, 0.5, 0.75, 1.0):
            row = wave_mod.render_idle_wave(40, 10, 22, intensity, wave_phase=26.0)[2]
            brightness = [styles._luma(color) for color in _lit_cell_colors(row)]
            assert brightness
            assert max(brightness) < min(brightness) * 2, (theme, intensity, brightness)
    finally:
        styles.apply_theme("default")


def test_reduced_color_ramp_only_fades_toward_its_dominant_channel() -> None:
    """Plain index scaling walks lime through green, *yellow*, then back to
    yellow-green. A ramp that doubles back reads as a color change, not depth."""
    for name in styles.THEME_NAMES:
        styles.apply_theme(name)
        for source in (styles.IDLE_WAVE_COLORS[2], styles.WAVE_SHADES[1]):
            ramp = styles.hue_ramp(source)
            lit = ramp[1:]
            assert len(lit) >= 2, (name, ramp)

            brightness = [styles._luma(step) for step in ramp]
            assert brightness == sorted(brightness), (name, ramp)

            ratios = [styles._hue_of(step) for step in lit]
            for channel in range(3):
                column = [ratio[channel] for ratio in ratios]
                assert column == sorted(column), (name, channel, ramp)
    styles.apply_theme("default")


def test_reduced_color_rows_never_go_dark(colored, palette) -> None:
    """The cube has no step below 0x5f on any hue, so the dimmest water is a
    stipple of the darkest lit step against black. That is honest — but if a
    row lands entirely on the black end the wave simply vanishes there."""
    for name in styles.THEME_NAMES:
        styles.apply_theme(name)
        for intensity in (0.0, 0.5, 1.0):
            for index, row in enumerate(
                wave_mod.render_idle_wave(
                    tick=40,
                    width=150,
                    height=22,
                    intensity=intensity,
                    wave_phase=26.0,
                )
            ):
                lit = _lit_cell_colors(row)
                if not lit:
                    continue
                dark = sum(1 for color in lit if sum(color) == 0)
                assert dark < 0.9 * len(lit), (name, intensity, index, dark, len(lit))
    styles.apply_theme("default")


def test_reduced_color_costs_no_more_output_than_truecolor(colored) -> None:
    """The idle menu reads keys and draws frames on one thread (``__main__``
    polls with a 70ms timeout, then paints), so a frame the terminal cannot
    drain blocks the write — and while it blocks, nothing reads the keyboard.

    Color changes are what cost bytes: a run of one color is nearly free, while
    alternating color spends an escape sequence per cell. Dithering once cost
    3030 escapes a frame at 300 columns and typing stopped appearing. Local
    caustic highlights add transitions, but the whole 300×60 frame must stay
    within 64 KiB and well below that previous escape count. Reduced color
    must not increase the output cost.
    """
    for name in styles.THEME_NAMES:
        styles.apply_theme(name)
        counts = []
        sizes = []
        for truecolor in (True, False):
            styles.TRUECOLOR = truecolor
            frame = "".join(
                wave_mod.render_idle_wave(
                    tick=40, width=300, height=60, intensity=0.75, wave_phase=26.0
                )
            )
            counts.append(frame.count("\033["))
            sizes.append(len(frame.encode()))
        assert max(counts) < 1600, (name, counts)
        assert max(sizes) < 64 * 1024, (name, sizes)
        assert counts[1] <= counts[0] * 1.2, (name, counts)
    styles.TRUECOLOR = True
    styles.apply_theme("default")


def test_truecolor_terminals_still_get_the_full_gradient(colored) -> None:
    rows = wave_mod.render_idle_wave(tick=40, width=120, height=20, intensity=0.75, wave_phase=26.0)
    colors = {color for row in rows for color in _rendered_colors(row)}

    assert not any("38;5;" in row for row in rows)
    assert len(colors) > len(styles.hue_ramp(styles.IDLE_WAVE_COLORS[2]))


def test_water_color_runs_keep_the_intended_shading(colored, monkeypatch) -> None:
    """Fewer terminal writes must not accumulate color error along a wave."""
    try:
        for theme in styles.THEME_NAMES:
            styles.apply_theme(theme)
            monkeypatch.setattr(wave_mod, "_COLOR_RUN_TOLERANCE", 0)
            exact = wave_mod.render_idle_wave(40, 118, 26, 0.75, 26.0)
            monkeypatch.setattr(wave_mod, "_COLOR_RUN_TOLERANCE", 4)
            compact = wave_mod.render_idle_wave(40, 118, 26, 0.75, 26.0)
            for expected, actual in zip(exact, compact, strict=True):
                assert re.sub(r"\033\[[0-9;]*m", "", expected) == re.sub(
                    r"\033\[[0-9;]*m", "", actual
                )
                for a, b in zip(_lit_cell_colors(expected), _lit_cell_colors(actual), strict=True):
                    assert max(abs(x - y) for x, y in zip(a, b, strict=True)) <= 4
            assert "".join(compact).count("\033[") < "".join(exact).count("\033[")
    finally:
        styles.apply_theme("default")


def test_colorterm_is_not_trusted_inside_a_multiplexer(monkeypatch) -> None:
    """COLORTERM is inherited by child processes, so inside tmux it describes
    the outer terminal rather than what tmux forwards."""
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.delenv("WAVEBENCH_COLOR_DEPTH", raising=False)

    monkeypatch.delenv("TMUX", raising=False)
    assert styles._detect_truecolor() is True

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    for features, expected in (("256,RGB,clipboard", True), ("256,clipboard", False)):
        monkeypatch.setattr(
            styles.subprocess,
            "run",
            lambda *a, _f=features, **k: SimpleNamespace(stdout=_f),
        )
        assert styles._detect_truecolor() is expected, features


def test_color_depth_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("WAVEBENCH_COLOR_DEPTH", "truecolor")
    assert styles._detect_truecolor() is True

    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("WAVEBENCH_COLOR_DEPTH", "256")
    assert styles._detect_truecolor() is False
