"""The idle-wave background must never leave a visible cursor on the water.

A wave frame is tens of kilobytes but a PTY accepts ~4 KB per write, so the
terminal receives one frame as several chunks and may repaint after *any* byte
of it.  If the cursor is visible at such a boundary the terminal stamps a block
cursor onto whatever wave cell the stream had reached — the white-on-black
artifact these tests exist to prevent.

The property under test is therefore about every prefix of the frame, not just
its endpoints: after any prefix, the cursor is either hidden or back at the
position the caller saved.
"""

from __future__ import annotations

import re

from wavebench.tui import styles
from wavebench.tui.progress import wave as wave_mod

_ESC = re.compile(r"\033\[[0-9;?]*[a-zA-Z]|\033[78]")


def _idle_frame(width: int = 200, height: int = 30, top: int = 10) -> str:
    """The exact string ``__main__._wave_idle`` hands to ``stdout.write``."""
    rows = wave_mod.render_idle_wave(3, width, height)
    body = "".join(f"\033[{top + i};2H{row}" for i, row in enumerate(rows))
    return styles.overlay_frame(body)


def _cursor_states(frame: str) -> list[tuple[bool, int, int]]:
    """Replay *frame*, yielding (visible, row, col) after every prefix.

    Only the states a terminal could actually render are reported: one entry
    per byte-prefix boundary, tracking DECTCEM visibility, DECSC/DECRC, CUP and
    plain glyph advance.
    """
    states: list[tuple[bool, int, int]] = []
    row = col = 1
    visible = True
    saved = (1, 1)
    i = 0
    while i < len(frame):
        match = _ESC.match(frame, i)
        if match:
            seq = match.group()
            i = match.end()
            if seq == "\0337":
                saved = (row, col)
            elif seq == "\0338":
                row, col = saved
            elif seq == styles.CURSOR_HIDE:
                visible = False
            elif seq == styles.CURSOR_SHOW:
                visible = True
            elif seq.endswith("H"):
                nums = seq[2:-1].split(";")
                row = int(nums[0] or 1)
                col = int(nums[1] or 1) if len(nums) > 1 else 1
        else:
            col += 1
            i += 1
        states.append((visible, row, col))
    return states


def test_overlay_frame_brackets_body_with_hide_and_sync() -> None:
    frame = styles.overlay_frame("BODY")

    assert frame == ("\033[?2026h\033[?25l\0337BODY\0338\033[?25h\033[?2026l")


def test_overlay_frame_hides_cursor_before_any_body_byte() -> None:
    """Order matters: a terminal ignoring ?2026 must see the hide first."""
    frame = styles.overlay_frame("BODY")

    assert frame.index(styles.CURSOR_HIDE) < frame.index("BODY")
    assert frame.index("BODY") < frame.index(styles.CURSOR_SHOW)


def test_overlay_frame_shows_cursor_only_after_position_restored() -> None:
    """Otherwise the cursor blinks back on while still parked on the water."""
    frame = styles.overlay_frame("BODY")

    assert frame.index("\0338") < frame.index(styles.CURSOR_SHOW)


def test_overlay_frame_hide_show_sits_inside_the_sync_block() -> None:
    """Terminals honouring ?2026 then never render the hidden state at all,
    so the prompt cursor does not strobe once per animation frame."""
    frame = styles.overlay_frame("BODY")

    assert frame.startswith(styles.SYNC_BEGIN)
    assert frame.endswith(styles.SYNC_END)
    assert frame.index(styles.SYNC_BEGIN) < frame.index(styles.CURSOR_HIDE)
    assert frame.index(styles.CURSOR_SHOW) < frame.index(styles.SYNC_END)


def test_cursor_control_survives_no_color() -> None:
    """NO_COLOR blanks palette escapes; hiding the cursor is not a colour."""
    assert styles.CURSOR_HIDE and styles.CURSOR_SHOW
    assert styles.SYNC_BEGIN and styles.SYNC_END


def test_idle_frame_never_leaves_a_visible_cursor_on_the_wave(monkeypatch) -> None:
    """The regression itself: no prefix of a real frame can paint a cursor
    anywhere inside the wave region."""
    monkeypatch.setattr(styles, "TRUECOLOR", True)
    monkeypatch.setattr(wave_mod, "_NO_COLOR", False)

    top = 10
    frame = _idle_frame(width=200, height=30, top=top)
    assert len(frame.encode()) > 4096, "frame must exceed one PTY write to be a real test"

    on_the_water = [
        (row, col) for visible, row, col in _cursor_states(frame) if visible and row >= top
    ]
    assert on_the_water == []


def test_unwrapped_idle_frame_would_leave_a_visible_cursor(monkeypatch) -> None:
    """Guards the guard — without the wrapper the replay must find artifacts,
    otherwise the test above proves nothing."""
    monkeypatch.setattr(styles, "TRUECOLOR", True)
    monkeypatch.setattr(wave_mod, "_NO_COLOR", False)

    rows = wave_mod.render_idle_wave(3, 200, 30)
    bare = "\0337" + "".join(f"\033[{10 + i};2H{row}" for i, row in enumerate(rows)) + "\0338"

    on_the_water = {
        (row, col) for visible, row, col in _cursor_states(bare) if visible and row >= 10
    }
    assert len(on_the_water) > 1000
