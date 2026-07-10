"""Focused tests for the layered idle-wave renderer."""

from __future__ import annotations

import re
from statistics import fmean

from wavebench.tui import styles
from wavebench.tui.progress import wave as wave_mod


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
