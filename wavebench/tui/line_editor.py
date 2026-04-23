"""Readline-style inline prompt editor.

``_read_line`` implements a one-line text input with:
  - left/right/home/end navigation
  - backspace/delete
  - Ctrl+A / Ctrl+E / Ctrl+K / Ctrl+U / Ctrl+W emacs editing bindings
  - up/down for history
  - bracketed-paste support (``\\033[200~ … \\033[201~``)
  - ``_TabEscape`` raised on Tab or Escape to signal navigation back

``_redraw_input`` is the redraw helper that scrolls horizontally when the
buffer is wider than the terminal, keeping the cursor visible.

Both require raw TTY access; in a non-TTY environment they degrade to the
built-in ``input()`` (no line editing).
"""

from __future__ import annotations

import os
import re
import select
import shutil
import sys
from collections.abc import Callable

try:
    import termios
    import tty

    _HAS_TTY = True
except ImportError:
    _HAS_TTY = False

from wavebench.tui.styles import _vlen


class _TabEscape(Exception):
    """Raised when Tab or Escape is pressed to navigate back."""

    pass


def _redraw_input(prompt: str, buf: list, cursor: int) -> None:
    """Redraw the input line, scrolling horizontally for long input."""
    term_w = shutil.get_terminal_size((80, 24)).columns
    prompt_w = _vlen(prompt)
    avail = max(1, term_w - prompt_w - 1)

    text = "".join(buf)

    if len(text) <= avail:
        visible = text
        vis_cursor = cursor
    else:
        half = avail // 2
        start = max(0, cursor - half)
        if start + avail > len(text):
            start = max(0, len(text) - avail)
        visible = text[start : start + avail]
        vis_cursor = cursor - start

    sys.stdout.write(f"\r{prompt}{visible}\033[K")
    back = len(visible) - vis_cursor
    if back > 0:
        sys.stdout.write(f"\033[{back}D")
    sys.stdout.flush()


def _read_line(
    prompt: str,
    history: list[str] | None = None,
    on_idle: Callable | None = None,
    idle_timeout: float = 0.07,
) -> str:
    """Read a line with basic editing and history.

    Supports left/right, home/end, backspace/delete, Ctrl+A/E/K/U/W,
    up/down for history, and raises *_TabEscape* on Tab or Escape.
    If *on_idle* is provided, it is called while waiting for input.
    """
    if not _HAS_TTY:
        return input(re.sub(r"\033\[[0-9;]*m", "", prompt))

    sys.stdout.write(prompt)
    sys.stdout.flush()

    buf: list[str] = []
    cursor = 0
    hist = list(history or [])
    hist.append("")
    hist_pos = len(hist) - 1

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    _idle_timeout = idle_timeout if on_idle else None
    try:
        tty.setraw(fd)
        sys.stdout.write("\033[?2004h")  # enable bracketed paste
        sys.stdout.flush()
        while True:
            ready, _, _ = select.select([fd], [], [], _idle_timeout)
            if not ready:
                if on_idle:
                    on_idle()
                continue
            raw = os.read(fd, 1)
            if not raw:
                continue
            b = raw[0]
            if b >= 0xC0:
                if b < 0xE0:
                    raw += os.read(fd, 1)
                elif b < 0xF0:
                    raw += os.read(fd, 2)
                else:
                    raw += os.read(fd, 3)
            ch = raw.decode("utf-8", errors="replace")

            if ch == "\x1b":
                esc_ready, _, _ = select.select([fd], [], [], 0.05)
                if not esc_ready:
                    raise _TabEscape()
                ch2 = os.read(fd, 1).decode("utf-8", errors="replace")
                if ch2 == "[":
                    ch3 = os.read(fd, 1).decode("utf-8", errors="replace")
                    if ch3 == "A" and hist_pos > 0:
                        hist[hist_pos] = "".join(buf)
                        hist_pos -= 1
                        buf = list(hist[hist_pos])
                        cursor = len(buf)
                        _redraw_input(prompt, buf, cursor)
                    elif ch3 == "B" and hist_pos < len(hist) - 1:
                        hist[hist_pos] = "".join(buf)
                        hist_pos += 1
                        buf = list(hist[hist_pos])
                        cursor = len(buf)
                        _redraw_input(prompt, buf, cursor)
                    elif ch3 == "C" and cursor < len(buf):
                        cursor += 1
                        sys.stdout.write("\033[C")
                        sys.stdout.flush()
                    elif ch3 == "D" and cursor > 0:
                        cursor -= 1
                        sys.stdout.write("\033[D")
                        sys.stdout.flush()
                    elif ch3 == "H":
                        cursor = 0
                        _redraw_input(prompt, buf, cursor)
                    elif ch3 == "F":
                        cursor = len(buf)
                        _redraw_input(prompt, buf, cursor)
                    elif ch3 in "2345678":
                        ch4 = os.read(fd, 1).decode("utf-8", errors="replace")
                        if ch3 == "3" and ch4 == "~" and cursor < len(buf):
                            buf.pop(cursor)
                            _redraw_input(prompt, buf, cursor)
                        elif ch3 == "2" and ch4 == "0":
                            # Bracketed paste: \033[200~ ... \033[201~
                            ch5 = os.read(fd, 1).decode("utf-8", errors="replace")
                            ch6 = os.read(fd, 1).decode("utf-8", errors="replace")
                            if ch5 == "0" and ch6 == "~":
                                paste_chars: list[str] = []
                                while True:
                                    pr = os.read(fd, 1)
                                    if not pr:
                                        break
                                    pb = pr[0]
                                    if pb >= 0xC0:
                                        if pb < 0xE0:
                                            pr += os.read(fd, 1)
                                        elif pb < 0xF0:
                                            pr += os.read(fd, 2)
                                        else:
                                            pr += os.read(fd, 3)
                                    pch = pr.decode("utf-8", errors="replace")
                                    if pch == "\x1b":
                                        p2 = os.read(fd, 1).decode("utf-8", errors="replace")
                                        if p2 == "[":
                                            p3 = os.read(fd, 1).decode("utf-8", errors="replace")
                                            p4 = os.read(fd, 1).decode("utf-8", errors="replace")
                                            p5 = os.read(fd, 1).decode("utf-8", errors="replace")
                                            p6 = os.read(fd, 1).decode("utf-8", errors="replace")
                                            if p3 + p4 + p5 + p6 == "201~":
                                                break
                                        continue
                                    if pch == "\r":
                                        continue  # skip CR (part of \r\n)
                                    if pch == "\n":
                                        paste_chars.append(" ")
                                    elif pch.isprintable():
                                        paste_chars.append(pch)
                                if paste_chars:
                                    buf[cursor:cursor] = paste_chars
                                    cursor += len(paste_chars)
                                    _redraw_input(prompt, buf, cursor)
                else:
                    raise _TabEscape()

            elif ch == "\t":
                raise _TabEscape()

            elif ch in ("\r", "\n"):
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return "".join(buf)

            elif ch == "\x03":
                raise KeyboardInterrupt()

            elif ch == "\x04" and not buf:
                raise EOFError()

            elif ch in ("\x7f", "\b") and cursor > 0:
                buf.pop(cursor - 1)
                cursor -= 1
                _redraw_input(prompt, buf, cursor)

            elif ch == "\x01":
                cursor = 0
                _redraw_input(prompt, buf, cursor)

            elif ch == "\x05":
                cursor = len(buf)
                _redraw_input(prompt, buf, cursor)

            elif ch == "\x0b":
                buf = buf[:cursor]
                _redraw_input(prompt, buf, cursor)

            elif ch == "\x15":
                buf = buf[cursor:]
                cursor = 0
                _redraw_input(prompt, buf, cursor)

            elif ch == "\x17":
                while cursor > 0 and buf[cursor - 1] == " ":
                    buf.pop(cursor - 1)
                    cursor -= 1
                while cursor > 0 and buf[cursor - 1] != " ":
                    buf.pop(cursor - 1)
                    cursor -= 1
                _redraw_input(prompt, buf, cursor)

            elif ch.isprintable():
                buf.insert(cursor, ch)
                cursor += 1
                # Defer redraw while more input is immediately available
                # (batches rapid input like paste in non-bracketed terminals)
                if not select.select([fd], [], [], 0)[0]:
                    _redraw_input(prompt, buf, cursor)
    except _TabEscape:
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
        raise
    except (KeyboardInterrupt, EOFError):
        sys.stdout.write("\r\n")
        sys.stdout.flush()
        raise
    finally:
        sys.stdout.write("\033[?2004l")  # disable bracketed paste
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
