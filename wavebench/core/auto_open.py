"""Auto-open subsystem — launches generated files in terminals/viewers.

Responsibilities:
  - Detect an available terminal emulator (``_find_terminal``).
  - Resolve the right interpreter for a file extension (``_resolve_interpreter``).
  - Build a shell command that runs a file and waits for Enter (``_shell_cmd``).
  - Open a file with the platform viewer (``_open_with_viewer``).
  - Run a single code file in a fresh terminal (``_run_in_terminal_single``).
  - Run a file as a tab in an existing terminal (``_open_file_in_tab``) or
    open multiple files as tabs in a single window (``_open_files_as_tabs``).

No internal imports from other ``wavebench.core.*`` modules — this is a
leaf of the dependency graph, consumed by ``core.runner`` and
``core.orchestrator``.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from typing import Any

# Extensions run with an interpreter in a new terminal window.
# None = use sys.executable (respects venvs, pyenv, conda, uv, etc.)
_INTERPRETER_MAP: dict[str, str | None] = {
    ".py": None,
    ".js": "node",
    ".sh": "bash",
}

# Terminal emulators: (binary, exec_flag, tab_flag_or_None)
_TERMINAL_SPECS: list[tuple] = [
    ("gnome-terminal", "--", "--tab"),
    ("konsole", "-e", "--new-tab"),
    ("xfce4-terminal", "-x", "--tab"),
    ("alacritty", "-e", None),
    ("kitty", None, None),
    ("xterm", "-e", None),
]


def _find_terminal() -> dict[str, Any] | None:
    """Find an available terminal emulator. Returns metadata dict, cached."""
    if hasattr(_find_terminal, "_cache"):
        return _find_terminal._cache  # type: ignore[attr-defined]

    result = None
    env = os.environ.get("TERMINAL")
    if env and shutil.which(env):
        result = {"name": env, "exec_flag": "-e", "tab_flag": None}
    else:
        # Try x-terminal-emulator first; resolve its real binary for tab lookup
        xte = shutil.which("x-terminal-emulator")
        if xte:
            real = os.path.basename(os.path.realpath(xte))
            tab_flag = None
            exec_flag = "-e"
            for name, ef, tf in _TERMINAL_SPECS:
                if real.startswith(name):
                    exec_flag, tab_flag = ef, tf
                    break
            result = {"name": "x-terminal-emulator", "exec_flag": exec_flag, "tab_flag": tab_flag}
        else:
            for name, ef, tf in _TERMINAL_SPECS:
                if shutil.which(name):
                    result = {"name": name, "exec_flag": ef, "tab_flag": tf}
                    break

    _find_terminal._cache = result  # type: ignore[attr-defined]
    return result


def _resolve_interpreter(filepath: str, ext: str) -> str | None:
    """Return the interpreter path for a code file extension."""
    if ext == ".py":
        return sys.executable
    name = _INTERPRETER_MAP.get(ext)
    if not name:
        return None
    return shutil.which(name)


def _shell_cmd(interp: str, filepath: str) -> str:
    """Build a bash command string that runs a file and waits for Enter."""
    return f'{shlex.quote(interp)} {shlex.quote(filepath)}; echo; read -rp "Press Enter to close…"'


def _open_with_viewer(filepath: str) -> None:
    """Open a file with the platform-native viewer, detached and silent."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(
                ["open", filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        elif sys.platform == "win32":
            os.startfile(filepath)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(
                ["xdg-open", filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except (OSError, FileNotFoundError):
        pass


def _open_file(filepath: str, interp: str | None = None) -> None:
    """Open a file: run code files with their interpreter, view everything else."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in _INTERPRETER_MAP:
        resolved = interp or _resolve_interpreter(filepath, ext)
        if resolved:
            _run_in_terminal_single(resolved, filepath)
        else:
            _open_with_viewer(filepath)
    else:
        _open_with_viewer(filepath)


def _run_in_terminal_single(interp: str, filepath: str) -> None:
    """Run a code file with its interpreter in a new terminal window (no tabs)."""
    try:
        if sys.platform == "darwin":
            cmd_str = f"{shlex.quote(interp)} {shlex.quote(filepath)}"
            osa_safe = cmd_str.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(
                ["osascript", "-e", f'tell application "Terminal" to do script "{osa_safe}"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        elif sys.platform == "win32":
            subprocess.Popen(
                ["cmd", "/c", "start", "cmd", "/k", interp, filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            info = _find_terminal()
            if not info:
                return
            cmd = [info["name"]]
            if info["exec_flag"]:
                cmd.append(info["exec_flag"])
            cmd += ["bash", "-c", _shell_cmd(interp, filepath)]
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except (OSError, FileNotFoundError):
        pass


# ── Tabbed terminal support ──────────────────────────────────────────────

_incremental_window_opened = False


def _reset_incremental_tabs() -> None:
    """Reset tab state at the start of each benchmark run."""
    global _incremental_window_opened
    _incremental_window_opened = False


def _open_file_in_tab(filepath: str, interp: str | None = None) -> None:
    """Open a file as a tab in the terminal window (incremental mode).

    First call opens a new window; subsequent calls add tabs.
    Falls back to separate windows on unsupported terminals or non-code files.
    """
    global _incremental_window_opened
    ext = os.path.splitext(filepath)[1].lower()

    if ext not in _INTERPRETER_MAP:
        _open_with_viewer(filepath)
        return

    resolved = interp or _resolve_interpreter(filepath, ext)
    if not resolved:
        _open_with_viewer(filepath)
        return

    if sys.platform != "linux":
        _run_in_terminal_single(resolved, filepath)
        _incremental_window_opened = True
        return

    info = _find_terminal()
    if not info or not info["tab_flag"]:
        _run_in_terminal_single(resolved, filepath)
        _incremental_window_opened = True
        return

    try:
        title = os.path.splitext(os.path.basename(filepath))[0]
        cmd = [info["name"]]
        if _incremental_window_opened:
            cmd.append(info["tab_flag"])
        if info["name"] in ("gnome-terminal", "x-terminal-emulator"):
            cmd += ["--title", title]
        if info["exec_flag"]:
            cmd.append(info["exec_flag"])
        cmd += ["bash", "-c", _shell_cmd(resolved, filepath)]
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _incremental_window_opened = True
    except (OSError, FileNotFoundError):
        pass


def _open_files_as_tabs(
    file_interps: list[tuple],
) -> None:
    """Open multiple code files as tabs in a single terminal window (after_all).

    *file_interps* is a list of ``(filepath, interpreter_path)`` tuples.
    Falls back to separate windows on unsupported terminals.
    """
    if not file_interps:
        return

    if sys.platform == "darwin":
        # macOS: build multi-tab AppleScript
        parts = []
        for i, (fp, interp) in enumerate(file_interps):
            cmd_str = f"{shlex.quote(interp)} {shlex.quote(fp)}"
            osa_safe = cmd_str.replace("\\", "\\\\").replace('"', '\\"')
            if i == 0:
                parts.append(f'tell application "Terminal" to do script "{osa_safe}"')
            else:
                parts.append(
                    f'tell application "Terminal" to do script "{osa_safe}" in front window'
                )
        try:
            script = "\n".join(parts)
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, FileNotFoundError):
            pass
        return

    if sys.platform == "win32":
        for fp, interp in file_interps:
            _run_in_terminal_single(interp, fp)
        return

    # Linux: batch tabs in one terminal command
    info = _find_terminal()
    if not info or not info["tab_flag"]:
        for fp, interp in file_interps:
            _run_in_terminal_single(interp, fp)
        return

    try:
        if info["name"] in ("gnome-terminal", "x-terminal-emulator"):
            # gnome-terminal: --tab --title T -- bash -c "cmd" (repeatable)
            cmd = [info["name"]]
            for fp, interp in file_interps:
                title = os.path.splitext(os.path.basename(fp))[0]
                cmd += ["--tab", "--title", title, "--", "bash", "-c", _shell_cmd(interp, fp)]
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        elif info["name"] == "xfce4-terminal":
            cmd = ["xfce4-terminal"]
            for fp, interp in file_interps:
                title = os.path.splitext(os.path.basename(fp))[0]
                cmd += ["--tab", "--title", title, "-x", "bash", "-c", _shell_cmd(interp, fp)]
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        elif info["name"] == "konsole":
            # konsole: first without --new-tab, subsequent with it
            for i, (fp, interp) in enumerate(file_interps):
                kcmd = ["konsole"]
                if i > 0:
                    kcmd.append("--new-tab")
                kcmd += ["-e", "bash", "-c", _shell_cmd(interp, fp)]
                subprocess.Popen(
                    kcmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
        else:
            # Unknown terminal with tab_flag: try per-file with tab flag
            for i, (fp, interp) in enumerate(file_interps):
                cmd = [info["name"]]
                if i > 0 and info["tab_flag"]:
                    cmd.append(info["tab_flag"])
                if info["exec_flag"]:
                    cmd.append(info["exec_flag"])
                cmd += ["bash", "-c", _shell_cmd(interp, fp)]
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
    except (OSError, FileNotFoundError):
        pass
