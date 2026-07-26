"""Focused tests for the layered idle-wave renderer and the bar waves."""

from __future__ import annotations

import re
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


_ANY_COLOR_OR_RESET = re.compile(
    r"\033\[(?:38;2;(\d+);(\d+);(\d+)|38;5;(\d+)|0)m"
)


def _rendered_colors(rendered: str) -> list[tuple[int, int, int]]:
    """Every color in a rendered wave, whichever escape form carries it."""
    return [
        _PALETTE[int(match.group(4))]
        if match.group(4)
        else tuple(map(int, match.group(1, 2, 3)))
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
        current = (
            (0, 0, 0) if match.group(1) is None else tuple(map(int, match.groups()))
        )
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
    return max(
        max(hue[i] for hue in hues) - min(hue[i] for hue in hues) for i in range(3)
    )


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


def test_surface_fill_mask_samples_both_braille_columns() -> None:
    mask = wave_mod._surface_fill_mask([0.2, 2.8], row=0, col=0)

    assert mask == 0xC7


def test_surface_contour_mask_follows_true_braille_geometry() -> None:
    mask = wave_mod._surface_contour_mask([0.2, 2.8], row=0, col=0)

    assert mask == 0x21


def test_foreground_edge_uses_body_color(monkeypatch) -> None:
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
            [2.4, 2.4, -1.0, -1.0],
        ),
    )

    row = wave_mod.render_idle_wave(tick=0, width=2, height=1, intensity=0.5)[0]
    visible = re.sub(r"\033\[[0-9;]*m", "", row)
    colors = re.findall(r"\033\[38;2;(\d+);(\d+);(\d+)m", row)

    assert visible == "⣤⣿"
    assert len(set(colors)) == 1


def test_background_contour_does_not_leak_across_foreground_edge(monkeypatch) -> None:
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


def test_far_contour_is_occluded_below_middle_surface(monkeypatch) -> None:
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

    assert visible == ["⠤", " "]


def test_render_idle_wave_uses_contours_behind_filled_foreground(monkeypatch) -> None:
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

    assert visible == "⠉⠒⣤"
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


def test_idle_wave_stays_on_a_single_hue(colored) -> None:
    sampled: list[tuple[int, int, int]] = []
    for intensity in (0.0, 0.5, 1.0):
        for row in wave_mod.render_idle_wave(
            tick=40, width=90, height=16, intensity=intensity, wave_phase=26.0
        ):
            sampled += [
                tuple(map(int, match))
                for match in re.findall(r"\033\[38;2;(\d+);(\d+);(\d+)m", row)
            ]

    assert _hue_drift(sampled) < 0.08


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
        steps = [abs(b - a) for a, b in zip(brightness, brightness[1:])]
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
    colors = _cell_colors(
        wave_mod._render_pulse_bar(1000, 1000, 30, phase=2.0, bar_width=28)
    )
    brightness = [sum(color) for color in colors]

    # A full bar ramps monotonically up to its leading edge, whatever shape the
    # wave happens to have underneath it.
    assert brightness == sorted(brightness)


# ── Depth ordering ────────────────────────────────────────────────────────


def test_rear_contours_never_outshine_the_foreground_body(colored) -> None:
    """A background contour brighter than the water in front of it inverts the
    depth cue and reads as a stray color rather than distance."""
    tick, width, height, phase = 40, 150, 20, 26.0

    for intensity in (0.0, 0.5, 1.0):
        _far, middle, foreground = wave_mod._idle_wave_surfaces(
            tick, width * 2, height, intensity, phase
        )
        rows = wave_mod.render_idle_wave(
            tick, width, height, intensity, wave_phase=phase
        )

        bodies: list[int] = []
        contours: list[int] = []
        for index, row in enumerate(rows):
            shades = sorted(
                {
                    tuple(map(int, match))
                    for match in re.findall(r"\033\[38;2;(\d+);(\d+);(\d+)m", row)
                },
                key=sum,
            )
            if not shades:
                continue
            submerged = any(
                wave_mod._surface_fill_mask(foreground, index, col)
                for col in range(width)
            )
            if submerged:
                bodies.append(sum(shades[-1]))
                contours += [sum(shade) for shade in shades[:-1]]
            else:
                contours += [sum(shade) for shade in shades]

        assert bodies, intensity
        assert max(contours, default=0) < min(bodies), intensity


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
                    tick=40, width=150, height=22,
                    intensity=intensity, wave_phase=26.0,
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
    alternating color spends an escape sequence per cell. Dithering between
    palette steps to smooth the gradient ran 3030 escapes a frame against 158 at
    300 columns, and typing stopped appearing. Depth varies down the rows, so
    each row must stay a flat run per layer however few palette steps there are.
    """
    for name in styles.THEME_NAMES:
        styles.apply_theme(name)
        counts = []
        for truecolor in (True, False):
            styles.TRUECOLOR = truecolor
            frame = "".join(
                wave_mod.render_idle_wave(
                    tick=40, width=300, height=60, intensity=0.75, wave_phase=26.0
                )
            )
            counts.append(frame.count("\033["))
        assert counts[1] <= counts[0] * 1.2, (name, counts)
    styles.TRUECOLOR = True
    styles.apply_theme("default")


def test_truecolor_terminals_still_get_the_full_gradient(colored) -> None:
    rows = wave_mod.render_idle_wave(
        tick=40, width=120, height=20, intensity=0.75, wave_phase=26.0
    )
    colors = {color for row in rows for color in _rendered_colors(row)}

    assert not any("38;5;" in row for row in rows)
    assert len(colors) > len(styles.hue_ramp(styles.IDLE_WAVE_COLORS[2]))


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
