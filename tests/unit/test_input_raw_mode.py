"""Typing during the animated idle screen must never be lost or kernel-echoed.

The idle screens poll ``_read_key_timeout`` while painting wave frames.  Three
termios behaviours conspire against that pattern:

  - ``tty.setraw`` defaults to ``TCSAFLUSH``, which *discards unread input* —
    a key typed between two polls is destroyed before the app can read it.
  - Between polls the tty is cooked with kernel ``ECHO`` on, so the terminal
    itself paints the key at whichever cell the cursor happens to occupy.
  - Each poll's ``TCSADRAIN`` restore blocks until the frame bytes drain.

The fixes under test: :func:`hold_raw` keeps the tty raw across a whole poll
loop (no flips, no echo windows), and every reader enters raw with ``TCSANOW``
so type-ahead survives.  These tests run against a real PTY because the
failure modes live in the kernel's tty layer, not in Python.
"""

from __future__ import annotations

import os
import pty
import select
import termios
import time
import tty

import pytest

from wavebench.tui.input import _read_key_timeout, hold_raw

# Generous margin for master→slave data to traverse the kernel's tty buffers.
_SETTLE = 0.05


class _PtyStdin:
    """Just enough of ``sys.stdin`` for the readers: a tty-backed fd."""

    def __init__(self, fd: int):
        self._fd = fd

    def fileno(self) -> int:
        return self._fd

    def isatty(self) -> bool:
        return True


@pytest.fixture
def pty_stdin(monkeypatch):
    """A real PTY wired in as stdin, starting cooked with ECHO on."""
    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[3] |= termios.ICANON | termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    monkeypatch.setattr("sys.stdin", _PtyStdin(slave))
    yield master, slave
    os.close(master)
    os.close(slave)


def _readable(fd: int, timeout: float = _SETTLE) -> bool:
    return bool(select.select([fd], [], [], timeout)[0])


def test_hold_raw_disables_canonical_and_echo_then_restores(pty_stdin) -> None:
    _, slave = pty_stdin
    before = termios.tcgetattr(slave)

    with hold_raw():
        lflag = termios.tcgetattr(slave)[3]
        assert not lflag & termios.ICANON, "input must not wait for a newline"
        assert not lflag & termios.ECHO, "the kernel must not paint keystrokes"

    assert termios.tcgetattr(slave) == before


def test_key_typed_before_a_poll_survives(pty_stdin) -> None:
    """The regression itself: cooked gap, key typed, next poll must read it.

    The old ``tty.setraw(fd)`` entry used TCSAFLUSH, which destroyed the
    queued key — on the idle screen every keypress landed in such a gap, so
    typing appeared dead.
    """
    master, _ = pty_stdin
    os.write(master, b"x")
    time.sleep(_SETTLE)

    assert _read_key_timeout(0.5) == "x"


def test_tcsaflush_would_have_discarded_that_key(pty_stdin) -> None:
    """Guards the guard: prove the setup above puts the key where the old
    TCSAFLUSH entry killed it — otherwise the test above proves nothing."""
    master, slave = pty_stdin
    os.write(master, b"x")
    time.sleep(_SETTLE)

    old = termios.tcgetattr(slave)
    try:
        tty.setraw(slave)  # the historical entry: default TCSAFLUSH
        assert not _readable(slave), "TCSAFLUSH no longer discards; test moot"
    finally:
        termios.tcsetattr(slave, termios.TCSANOW, old)


def test_key_typed_before_hold_raw_survives_entry(pty_stdin) -> None:
    """``hold_raw`` enters with TCSANOW for the same reason."""
    master, _ = pty_stdin
    os.write(master, b"3")
    time.sleep(_SETTLE)

    with hold_raw():
        assert _read_key_timeout(0.5) == "3"


def test_poll_inside_hold_raw_never_flips_the_mode(pty_stdin) -> None:
    """Held polls must be pure reads: no mode change on entry (which would
    flush) and none on exit (whose TCSADRAIN stalled on wave frames)."""
    _, slave = pty_stdin

    with hold_raw():
        held = termios.tcgetattr(slave)
        assert _read_key_timeout(0.01) is None  # timeout path
        assert termios.tcgetattr(slave) == held


def test_no_kernel_echo_while_held(pty_stdin) -> None:
    """Nothing typed during the idle loop may be painted by the kernel."""
    master, _ = pty_stdin

    with hold_raw():
        os.write(master, b"z")
        assert _read_key_timeout(0.5) == "z"
        assert not _readable(master), "kernel echoed the key back"


def test_kernel_echo_returns_after_release(pty_stdin) -> None:
    """Guards the guard: with the cooked attrs restored, the same keystroke
    does echo — so the silence above is hold_raw's doing, not the PTY's."""
    master, _ = pty_stdin

    with hold_raw():
        pass
    os.write(master, b"q")

    assert _readable(master), "echo expected once cooked mode is restored"
    assert os.read(master, 1) == b"q"


def test_hold_raw_is_a_noop_without_a_tty(monkeypatch) -> None:
    """Piped stdin (tests, CI, ``wavebench < file``) must pass straight through."""

    class _NotATty:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("sys.stdin", _NotATty())
    with hold_raw():
        pass


def test_read_key_timeout_restores_cooked_mode_when_not_held(pty_stdin) -> None:
    """Standalone calls must still clean up after themselves."""
    _, slave = pty_stdin
    before = termios.tcgetattr(slave)

    assert _read_key_timeout(0.01) is None

    assert termios.tcgetattr(slave) == before
