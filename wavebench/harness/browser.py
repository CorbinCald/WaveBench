"""Open host-browser previews without sending browser diagnostics to the TUI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_LAUNCH_TIMEOUT = 10
_OPEN = "import sys, webbrowser; sys.exit(0 if webbrowser.open(sys.argv[1]) else 1)"


def open_preview(url: str, log_path: Path) -> bool:
    """Keep platform/BROWSER selection, isolating its stdout/stderr in a log.

    Run webbrowser in a helper so redirecting its native child processes never
    changes the controller's file descriptors during concurrent model runs.
    Browsers that outlive the helper keep writing to the inherited log handle.
    """
    try:
        with log_path.open("ab", buffering=0) as log:
            log.write(f"Opening preview: {url}\n".encode())
            try:
                result = subprocess.run(
                    [sys.executable, "-I", "-c", _OPEN, url],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    timeout=_LAUNCH_TIMEOUT,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.write(f"Browser launch failed: {exc}\n".encode("utf-8", errors="replace"))
                return False
            if result.returncode:
                log.write(f"Browser launcher exited with status {result.returncode}\n".encode())
            return result.returncode == 0
    except OSError:
        # A failed log open must not fall back to leaking output into the TUI.
        return False
