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
"""

from __future__ import annotations

import os
import select
import sys

try:
    import termios
    import tty

    _HAS_TTY = True
except ImportError:
    _HAS_TTY = False


def _read_key() -> str:
    """Read a single keypress from the terminal, handling escape sequences."""
    if not _HAS_TTY:
        ch = input()[:1]
        return ch or "enter"
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
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
        tty.setraw(fd)
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
    """Read a single keypress, returning ``None`` if *timeout_s* elapses."""
    if not _HAS_TTY or not sys.stdin.isatty():
        ch = input()[:1]
        return ch or "enter"
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
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
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
