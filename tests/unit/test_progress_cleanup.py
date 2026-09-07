"""A failed display must leave the user's terminal and print function usable."""

from __future__ import annotations

import asyncio
import builtins

import pytest

from wavebench.tui.progress.tracker import ProgressTracker


@pytest.mark.parametrize("failure", ["animation", "final"])
async def test_failed_renderer_restores_terminal_and_print(monkeypatch, capsys, failure):
    original_print = builtins.print
    tracker = ProgressTracker(1, {}, alt_screen=True)
    tracker._is_tty = tracker._alt_screen = True

    async def animate():
        if failure == "animation":
            raise RuntimeError("display failed")
        await asyncio.Event().wait()

    def final():
        raise RuntimeError("display failed")

    monkeypatch.setattr(tracker, "_animate", animate)
    if failure == "final":
        monkeypatch.setattr(tracker, "_render_final", final)
    await tracker.start()
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="display failed"):
        await tracker.stop()

    assert builtins.print is original_print
    assert not tracker.is_running
    assert tracker._task is None
    assert not tracker._entered_alt_screen
    output = capsys.readouterr().out
    assert "\033[?1049h" in output
    assert "\033[?25h\033[?1049l" in output
    await tracker.stop()
