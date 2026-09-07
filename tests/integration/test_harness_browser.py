"""Real browser-launch subprocesses must never write into WaveBench's terminal."""

from __future__ import annotations

import os
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from wavebench.harness import browser


@pytest.fixture
def browser_command(tmp_path, monkeypatch):
    # A real subprocess selected through the same BROWSER mechanism as users.
    # Disable desktop/text fallbacks so a failure cannot open the host browser.
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    script = tmp_path / "test browser.py"
    monkeypatch.setenv("BROWSER", shlex.join([sys.executable, str(script), "%s"]))
    return script


def test_concurrent_browser_logs_capture_late_descendants_without_capturing_tui(
    browser_command, tmp_path, capfd
):
    browser_command.write_text(
        "import subprocess, sys\n"
        "print('browser stdout ' + sys.argv[1], flush=True)\n"
        "print('browser stderr ' + sys.argv[1], file=sys.stderr, flush=True)\n"
        "child = '''import sys, time\n"
        "from pathlib import Path\n"
        "deadline = time.monotonic() + 5\n"
        "while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "print('late browser stdout ' + sys.argv[1], flush=True)\n"
        "print('late browser stderr ' + sys.argv[1], file=sys.stderr, flush=True)\n"
        "'''\n"
        "subprocess.Popen([sys.executable, '-c', child, sys.argv[1], __file__ + '.release'], start_new_session=True)\n",
        encoding="utf-8",
    )
    urls = [f"http://127.0.0.1:8000/{name}?a=1&b=2" for name in ("first", "second")]
    logs = [tmp_path / f"model {i}.log" for i in range(2)]
    release = Path(str(browser_command) + ".release")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(browser.open_preview, url, log)
                for url, log in zip(urls, logs, strict=True)
            ]
            os.write(1, b"WaveBench progress still visible\n")
            assert all(future.result(timeout=10) for future in futures)
        # Both launch helpers exited, but background browser processes are alive.
        assert all("late browser" not in log.read_text() for log in logs)
        release.touch()
        deadline = time.monotonic() + 5
        while not all("late browser stderr" in log.read_text() for log in logs):
            assert time.monotonic() < deadline, "background browser logging stalled"
            time.sleep(0.01)
        for index, (url, log) in enumerate(zip(urls, logs, strict=True)):
            contents = log.read_text()
            assert f"browser stdout {url}" in contents
            assert f"browser stderr {url}" in contents
            assert f"late browser stdout {url}" in contents
            assert f"late browser stderr {url}" in contents
            assert urls[1 - index] not in contents
            assert "WaveBench progress" not in contents
        terminal = capfd.readouterr()
        assert terminal.out == "WaveBench progress still visible\n"
        assert terminal.err == ""
    finally:
        release.touch()


def test_browser_failure_is_reported_and_logged(browser_command, tmp_path, capfd):
    browser_command.write_text(
        "import sys\nprint('browser could not start', file=sys.stderr)\nsys.exit(9)\n",
        encoding="utf-8",
    )
    log = tmp_path / "browser.log"
    assert not browser.open_preview("http://127.0.0.1:8000/", log)
    assert "browser could not start" in log.read_text()
    assert "Browser launcher exited with status 1" in log.read_text()
    assert capfd.readouterr() == ("", "")


def test_launch_timeout_is_reported_in_log(tmp_path, monkeypatch, capfd):
    monkeypatch.setattr(browser, "_OPEN", "import time; time.sleep(30)")
    monkeypatch.setattr(browser, "_LAUNCH_TIMEOUT", 0.1)
    log = tmp_path / "browser.log"
    assert not browser.open_preview("http://127.0.0.1:8000/", log)
    assert "timed out" in log.read_text()
    assert capfd.readouterr() == ("", "")


def test_log_open_failure_does_not_launch_browser(tmp_path, monkeypatch, capfd):
    def unexpected_launch(*args, **kwargs):
        pytest.fail("browser must not launch without its output redirected")

    monkeypatch.setattr(browser.subprocess, "run", unexpected_launch)
    assert not browser.open_preview("http://127.0.0.1:8000/", tmp_path / "missing" / "browser.log")
    assert capfd.readouterr() == ("", "")
