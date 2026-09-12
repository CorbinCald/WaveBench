"""Exercise the actual configuration menu and persistence through a real PTY."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize("columns", [48, 100])
def test_destination_cycle_save_reopen_and_cancel(tmp_path, columns):
    repo = Path(__file__).resolve().parents[2]
    code = (
        "from wavebench.storage import load_config,save_config; "
        "from wavebench.tui.menus.config_menu import interactive_config_menu; "
        "_,config=interactive_config_menu([],{'Fixture':'test/model'},load_config()); "
        "save_config(config) if config is not None else None"
    )
    env = {**os.environ, "PYTHONPATH": str(repo), "TERM": "xterm-256color"}
    config_path = tmp_path / ".benchmark_config.json"
    config_path.write_text(json.dumps({"auto_open": "after_all"}))

    def interact(spaces, save):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, columns, 0, 0))
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=tmp_path,
            env=env,
        )
        os.close(slave)
        output = bytearray()

        def drain():
            deadline = time.monotonic() + 5
            seen = False
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1 if seen else 1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    output.extend(chunk)
                    seen = True
                elif seen:
                    break

        try:
            drain()
            for key in [b"\x1b[C"] * 3 + [b"\x1b[B"] * 5:
                os.write(master, key)
                drain()
            assert b"Preview destination" in output
            for _ in range(spaces):
                os.write(master, b" ")
                drain()
            os.write(master, b"\r" if save else b"\x1b")
            drain()
            assert proc.wait(timeout=5) == 0, output.decode(errors="replace")
            return output.decode(errors="replace")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)

    text = interact(1, True)
    assert "Connected laptop" in text
    assert json.loads(config_path.read_text())["preview_destination"] == "laptop"
    before = config_path.read_bytes()
    assert "Wavebench host" in interact(1, False)
    assert config_path.read_bytes() == before
    assert "Automatic" in interact(2, True)
    assert json.loads(config_path.read_text())["preview_destination"] == "automatic"
    assert json.loads(config_path.read_text())["auto_open"] == "after_all"
