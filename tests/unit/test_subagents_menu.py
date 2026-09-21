"""The Subagents setup screen edits the run setting, parallel window, and total cap."""

from __future__ import annotations

import sys
from contextlib import nullcontext

import pytest

from wavebench.tui.menus import subagents_menu as menu


def keys(monkeypatch, values):
    values = iter(values)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(menu, "hold_raw", nullcontext)
    monkeypatch.setattr(menu, "_read_key_or_resize", lambda: next(values))


def test_enable_cycle_parallel_and_type_a_cap(monkeypatch, capsys):
    keys(monkeypatch, ["space", "down", "space", "space", "down", "ctrl-a", "1", "2", "enter"])
    config = {"theme": "default", "subagents": "off", "harness": {"build_seconds": 5}}
    updated = menu.interactive_subagents(config)
    assert updated == {
        "theme": "default",
        "subagents": "on",
        "harness": {"build_seconds": 5, "subagent_parallel": 2, "subagent_cap": 12},
    }
    assert config["subagents"] == "off" and "subagent_cap" not in config["harness"]
    output = capsys.readouterr().out
    assert "On (2 parallel, 12 total)" in output and "\033[?1049h" in output


def test_arrows_clamp_parallel_and_cap_and_escape_cancels(monkeypatch, capsys):
    keys(
        monkeypatch,
        ["down", "left", "left", "left", "down", "left", "left", "right", "resize", "escape"],
    )
    config = {"subagents": "on", "harness": {"subagent_parallel": 4, "subagent_cap": 2}}
    assert menu.interactive_subagents(config, alternate_screen=False) is None
    output = capsys.readouterr().out
    assert "\033[?1049h" not in output
    # Parallel wraps within 2-5 (4 → 3 → 2 → 5); the cap never drops below 1 (2 → 1 → 1 → 2).
    assert "On (5 parallel, 2 total)" in output


def test_empty_or_zero_cap_is_rejected_until_corrected(monkeypatch, capsys):
    keys(
        monkeypatch, ["down", "down", "backspace", "enter", "0", "enter", "backspace", "7", "enter"]
    )
    updated = menu.interactive_subagents({"subagents": "on"})
    assert updated["harness"] == {"subagent_parallel": 4, "subagent_cap": 7}
    output = capsys.readouterr().out
    assert "Enter a total agent cap from 1 to 9,999." in output
    assert "On (cap needed)" in output


def test_disable_keeps_caps_for_later(monkeypatch):
    keys(monkeypatch, ["space", "enter"])
    config = {"subagents": "on", "harness": {"subagent_parallel": 5, "subagent_cap": 3}}
    assert menu.interactive_subagents(config) == {
        "subagents": "off",
        "harness": {"subagent_parallel": 5, "subagent_cap": 3},
    }


def test_invalid_saved_parallel_falls_back_to_the_default(monkeypatch):
    keys(monkeypatch, ["enter"])
    config = {"subagents": "on", "harness": {"subagent_parallel": 9, "subagent_cap": 3}}
    assert menu.interactive_subagents(config)["harness"]["subagent_parallel"] == 4


def test_non_terminal_explains_the_flags(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert menu.interactive_subagents({}) is None
    assert "--agent-cap" in capsys.readouterr().out


@pytest.mark.parametrize("key", ["escape", "ctrl-c"])
def test_cancel_keys_never_change_the_config(monkeypatch, key):
    keys(monkeypatch, ["space", key])
    assert menu.interactive_subagents({"subagents": "off"}) is None
