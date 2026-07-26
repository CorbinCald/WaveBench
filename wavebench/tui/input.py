"""Raw keyboard input primitives used across every TUI screen.

Three flavors:

  - ``_read_key()`` — blocking read via ``sys.stdin`` in raw mode.
  - ``_read_key_or_resize(winch_r)`` — blocking via ``select`` plus an
    optional SIGWINCH pipe so window-resize events can unblock the read.
  - ``_read_key_timeout(timeout_s)`` — returns ``None`` after *timeout_s*
    if no key is ready, enabling animated idle screens that poll for input.

All three normalize keypresses to short identifier strings ("up", "down",
"enter", "tab", "space", "backspace", "escape", "ctrl-a", "ctrl-c",
"ctrl-n") or return the raw character for anything else.

Loops that poll ``_read_key_timeout`` while animating should run inside
:func:`hold_raw`, which keeps the tty raw across the whole loop instead of
flipping modes on every call.
"""

from __future__ import annotations

import contextlib
import os
import select
import sys

try:
    import termios
    import tty

    _HAS_TTY = True
except ImportError:
    _HAS_TTY = False


@contextlib.contextmanager
def hold_raw():
    """Keep the tty in raw mode across a whole key-wait loop.

    ``_read_key_timeout`` historically flipped raw mode per call. Between
    calls the tty is cooked, and re-entering raw goes through ``tty.setraw``,
    whose default ``TCSAFLUSH`` *discards unread input* — so a key typed in
    the gap is destroyed before the app can read it. Under an animated idle
    screen the gaps are wide: the per-call restore uses ``TCSADRAIN``, which
    blocks until the whole animation frame has drained to the terminal. On
    top of that, the cooked windows have kernel ``ECHO`` enabled, painting
    keystrokes wherever the cursor happens to sit — including mid-frame.

    Holding raw once for the whole loop removes the gaps entirely; the
    per-call helpers detect the held state and skip their own mode changes.
    Entry uses ``TCSANOW`` so a key typed just before the loop survives.
    While held, ``OPOST`` is off — write ``\\r\\n``, not ``\\n``, and prefer
    cursor-addressed output.
    """
    if not _HAS_TTY or not sys.stdin.isatty():
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd, when=termios.TCSANOW)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_key() -> str:
    """Read a single keypress from the terminal, handling escape sequences."""
    if not _HAS_TTY:
        ch = input()[:1]
        return ch or "enter"
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        # TCSANOW, not the default TCSAFLUSH: flushing would destroy any key
        # the user typed ahead of this call.
        tty.setraw(fd, when=termios.TCSANOW)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(ch3, "")
            return "escape"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\t":
            return "tab"
        if ch == " ":
            return "space"
        if ch == "\x03":
            return "ctrl-c"
        if ch == "\x01":
            return "ctrl-a"
        if ch == "\x0e":
            return "ctrl-n"
        if ch in ("\x7f", "\b"):
            return "backspace"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_key_or_resize(winch_r: int = -1) -> str:
    """Read a single keypress, returning ``'resize'`` if SIGWINCH fires."""
    if not _HAS_TTY:
        ch = input()[:1]
        return ch or "enter"
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        # TCSANOW: keep type-ahead — a key typed while the previous one was
        # being handled must survive this re-entry into raw mode.
        tty.setraw(fd, when=termios.TCSANOW)
        watch: list = [fd]
        if winch_r >= 0:
            watch.append(winch_r)
        try:
            ready, _, _ = select.select(watch, [], [])
        except (OSError, ValueError):
            return "resize"
        if winch_r >= 0 and winch_r in ready:
            try:
                os.read(winch_r, 1024)
            except OSError:
                pass
            return "resize"
        ch = os.read(fd, 1)
        if not ch:
            return ""
        ch = ch.decode("utf-8", errors="replace")
        if ch == "\x1b":
            esc_ready, _, _ = select.select([fd], [], [], 0.05)
            if not esc_ready:
                return "escape"
            ch2 = os.read(fd, 1).decode("utf-8", errors="replace")
            if ch2 == "[":
                ch3 = os.read(fd, 1).decode("utf-8", errors="replace")
                return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(ch3, "")
            return "escape"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\t":
            return "tab"
        if ch == " ":
            return "space"
        if ch == "\x03":
            return "ctrl-c"
        if ch == "\x01":
            return "ctrl-a"
        if ch == "\x0e":
            return "ctrl-n"
        if ch in ("\x7f", "\b"):
            return "backspace"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_key_timeout(timeout_s: float = 0.08) -> str | None:
    """Read a single keypress, returning ``None`` if *timeout_s* elapses.

    Inside :func:`hold_raw` this becomes a pure poll — no mode flips, so no
    ``TCSAFLUSH`` input discard and no ``TCSADRAIN`` stall per call.
    """
    if not _HAS_TTY or not sys.stdin.isatty():
        ch = input()[:1]
        return ch or "enter"
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    already_raw = not (old[3] & termios.ICANON)
    try:
        if not already_raw:
            # TCSANOW: the default TCSAFLUSH would discard a key typed just
            # before this call — the historical cause of "typing does nothing"
            # on the animated idle screen.
            tty.setraw(fd, when=termios.TCSANOW)
        ready, _, _ = select.select([fd], [], [], timeout_s)
        if not ready:
            return None
        ch = os.read(fd, 1)
        if not ch:
            return None
        ch = ch.decode("utf-8", errors="replace")
        if ch == "\x1b":
            esc_ready, _, _ = select.select([fd], [], [], 0.05)
            if not esc_ready:
                return "escape"
            ch2 = os.read(fd, 1).decode("utf-8", errors="replace")
            if ch2 == "[":
                ch3 = os.read(fd, 1).decode("utf-8", errors="replace")
                return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(ch3, "")
            return "escape"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\t":
            return "tab"
        if ch == " ":
            return "space"
        if ch == "\x03":
            return "ctrl-c"
        if ch == "\x01":
            return "ctrl-a"
        if ch == "\x0e":
            return "ctrl-n"
        if ch in ("\x7f", "\b"):
            return "backspace"
        return ch
    finally:
        if not already_raw:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
