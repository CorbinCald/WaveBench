import os
import re
import sys
import json
import time
import shlex
import shutil
import asyncio
import subprocess
import aiohttp
from typing import Dict, Any, Optional, List

from wavebench.api import call_model_streaming, call_model_async
from wavebench.parsers import parse_llm_output, get_directory_name
from wavebench.tui.styles import (
    S, _wait, _fail, _work, _ok, _skip, _arrow, format_duration, format_cost,
    _truncate, _dot, _rpad, _tw, _vlen,
    _box, _box_top, _box_row, _box_sep, _box_bot, _box_divider,
)
import wavebench.tui.styles as _styles
from wavebench.tui.components import ProgressTracker, display_analytics, compute_cost
from wavebench.storage import load_history, record_run

OUTPUT_DIR       = "benchmarkResults"
MAX_CONCURRENCY  = 12
REQUEST_TIMEOUT  = 1800  # seconds

SYSTEM_PROMPT_CODE = (
    "You are an expert programmer. Your goal is to provide a complete, "
    "fully functional, single-file implementation based on the user's request. "
    "Do not include any external modules or dependencies. "
    "Return ONLY the code, with no preamble or explanation."
)

SYSTEM_PROMPT_CODE_DEPS = (
    "You are an expert programmer. Your goal is to provide a complete, "
    "fully functional, single-file implementation based on the user's request. "
    "You may use third-party packages from PyPI if they are helpful. "
    "Return ONLY the code, with no preamble or explanation."
)

SYSTEM_PROMPT_TEXT = (
    "You are a knowledgeable assistant. Provide a clear, detailed, and "
    "well-structured answer to the user's question. Use Markdown formatting "
    "for readability. Do not include code unless the user explicitly asks for it."
)

def get_unique_filename(directory: str, base_name: str, extension: str) -> str:
    """Return a unique filename, appending _v2, _v3, … if needed."""
    base = re.sub(r'cursor', '', base_name, flags=re.IGNORECASE)
    if not extension.startswith('.'):
        extension = f".{extension}"

    path = os.path.join(directory, f"{base}{extension}")
    if not os.path.exists(path):
        return f"{base}{extension}"

    counter = 2
    while True:
        name = f"{base}_v{counter}{extension}"
        if not os.path.exists(os.path.join(directory, name)):
            return name
        counter += 1

# Extensions run with an interpreter in a new terminal window.
# None = use sys.executable (respects venvs, pyenv, conda, uv, etc.)
_INTERPRETER_MAP: Dict[str, Optional[str]] = {
    ".py": None,
    ".js": "node",
    ".sh": "bash",
}

# Terminal emulators: (binary, exec_flag, tab_flag_or_None)
_TERMINAL_SPECS: List[tuple] = [
    ("gnome-terminal", "--",  "--tab"),
    ("konsole",        "-e",  "--new-tab"),
    ("xfce4-terminal", "-x",  "--tab"),
    ("alacritty",      "-e",  None),
    ("kitty",          None,  None),
    ("xterm",          "-e",  None),
]

_DEP_DETECT_MODEL = "google/gemini-2.5-flash-lite"


def _find_terminal() -> Optional[Dict[str, Any]]:
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
            result = {"name": "x-terminal-emulator", "exec_flag": exec_flag,
                      "tab_flag": tab_flag}
        else:
            for name, ef, tf in _TERMINAL_SPECS:
                if shutil.which(name):
                    result = {"name": name, "exec_flag": ef, "tab_flag": tf}
                    break

    _find_terminal._cache = result  # type: ignore[attr-defined]
    return result


def _resolve_interpreter(filepath: str, ext: str) -> Optional[str]:
    """Return the interpreter path for a code file extension."""
    if ext == ".py":
        return sys.executable
    name = _INTERPRETER_MAP.get(ext)
    if not name:
        return None
    return shutil.which(name)


def _shell_cmd(interp: str, filepath: str) -> str:
    """Build a bash command string that runs a file and waits for Enter."""
    return (
        f"{shlex.quote(interp)} {shlex.quote(filepath)}"
        f'; echo; read -rp "Press Enter to close…"'
    )


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


def _open_file(filepath: str, interp: Optional[str] = None) -> None:
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
                ["osascript", "-e",
                 f'tell application "Terminal" to do script "{osa_safe}"'],
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


def _open_file_in_tab(filepath: str, interp: Optional[str] = None) -> None:
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
    file_interps: List[tuple],
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
                parts.append(
                    f'tell application "Terminal" to do script "{osa_safe}"')
            else:
                parts.append(
                    f'tell application "Terminal" to do script "{osa_safe}" '
                    f'in front window')
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
                cmd += ["--tab", "--title", title, "--",
                        "bash", "-c", _shell_cmd(interp, fp)]
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
                cmd += ["--tab", "--title", title, "-x",
                        "bash", "-c", _shell_cmd(interp, fp)]
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


# ── Dependency detection & venv management ───────────────────────────────

_venv_lock = asyncio.Lock()


async def _detect_dependencies(
    session: aiohttp.ClientSession,
    api_key: str,
    code: str,
) -> List[str]:
    """Use a fast LLM to detect third-party package imports in Python code."""
    prompt = (
        "Analyze this Python code and list any third-party packages (NOT in the "
        "standard library) it imports. Map import names to their pip package names "
        "(e.g. cv2 -> opencv-python, PIL -> Pillow, sklearn -> scikit-learn, "
        "bs4 -> beautifulsoup4, yaml -> pyyaml, dotenv -> python-dotenv).\n"
        'Return ONLY a JSON object: {"packages": ["pkg1", "pkg2"]}\n'
        'If no third-party packages are needed, return: {"packages": []}\n\n'
        f"```python\n{code[:4000]}\n```"
    )
    try:
        raw = await call_model_async(
            session, api_key, _DEP_DETECT_MODEL, prompt,
            reasoning_effort=None, max_tokens=256,
        )
        if not raw:
            return []
        # Parse JSON from potentially noisy response
        text = raw.strip()
        # Try to extract JSON object
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            data = json.loads(text[start:end + 1])
            pkgs = data.get("packages", [])
            if isinstance(pkgs, list):
                return [p for p in pkgs if isinstance(p, str) and p.strip()]
    except Exception:
        pass
    return []


async def _ensure_venv(output_dir: str) -> str:
    """Create a shared venv in the output directory. Returns the venv Python path."""
    venv_dir = os.path.join(output_dir, ".venv")
    if sys.platform == "win32":
        venv_python = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        venv_python = os.path.join(venv_dir, "bin", "python")

    if os.path.isfile(venv_python):
        return venv_python

    async with _venv_lock:
        # Double-check after acquiring lock
        if os.path.isfile(venv_python):
            return venv_python
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "venv", venv_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"venv creation failed: {stderr.decode(errors='replace')}")
    return venv_python


# Packages that may lack pre-built wheels on newer Python versions.
# Maps the original package to a list of alternatives to try in order.
_PACKAGE_FALLBACKS: Dict[str, List[str]] = {
    "pygame": ["pygame-ce", "pygame"],
}


async def _install_packages(venv_python: str, packages: List[str]) -> bool:
    """Install packages into a venv via pip, with fallbacks for known problem packages."""
    if not packages:
        return True
    all_ok = True
    for pkg in packages:
        candidates = _PACKAGE_FALLBACKS.get(pkg, [pkg])
        installed = False
        for candidate in candidates:
            proc = await asyncio.create_subprocess_exec(
                venv_python, "-m", "pip", "install", "--quiet", candidate,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                installed = True
                break
        if not installed:
            all_ok = False
    return all_ok


def _venv_python_path(output_dir: str) -> str:
    """Return the expected venv Python path for an output directory."""
    venv_dir = os.path.join(output_dir, ".venv")
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")

async def process_model(session: aiohttp.ClientSession, api_key: str, model_name: str, model_id: str, prompt: str,
                        default_ext: str, output_dir_task: asyncio.Task, semaphore: asyncio.Semaphore,
                        results: Dict[str, Any], pad: int, tracker: Any,
                        reasoning_effort: Optional[str] = "high",
                        auto_open: str = "off",
                        auto_install: str = "off") -> None:
    """Generate code from a single model, parse it, and save to disk."""
    start = time.monotonic()
    registered = tracker.is_running

    if registered:
        tracker.register(model_name)
    else:
        print(f"  {_wait} {model_name:<{pad}}  "
              f"{S.DIM}calling {model_id}…{S.RST}")

    content: Optional[str] = None
    usage: dict = {}

    try:
        async with semaphore:
            def _on_progress(chars: int) -> None:
                if registered:
                    tracker.update(model_name, chars)

            content, usage = await call_model_streaming(
                session, api_key, model_id, prompt,
                reasoning_effort=reasoning_effort,
                on_progress=_on_progress)

    except asyncio.CancelledError:
        elapsed = time.monotonic() - start
        if not registered:
            print(f"  {_skip} {model_name:<{pad}}  "
                  f"{S.DIM}cancelled  [{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "cancelled", "time_s": elapsed, "file": None, "usage": {}}
        return
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        if not registered:
            print(f"  {_fail} {model_name:<{pad}}  "
                  f"{S.RED}timeout{S.RST}  "
                  f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return
    except aiohttp.ClientError as exc:
        elapsed = time.monotonic() - start
        if not registered:
            print(f"  {_fail} {model_name:<{pad}}  "
                  f"{S.RED}API error: {exc}{S.RST}  "
                  f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return
    except Exception as exc:
        elapsed = time.monotonic() - start
        exc_str = str(exc) or exc.__class__.__name__
        if not registered:
            print(f"  {_fail} {model_name:<{pad}}  "
                  f"{S.RED}{exc_str}{S.RST}  "
                  f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return
    finally:
        if registered:
            tracker.unregister(model_name)

    elapsed = time.monotonic() - start

    if not content:
        if not registered:
            print(f"  {_fail} {model_name:<{pad}}  "
                  f"{S.RED}no response{S.RST}  "
                  f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return

    if registered:
        tracker.mark_parsing(model_name)
    else:
        print(f"  {_work} {model_name:<{pad}}  "
              f"{S.DIM}parsing…{S.RST}")

    parsed = await parse_llm_output(
        session, api_key, model_name, content)

    if registered:
        tracker.finish_parsing(model_name)

    if parsed and parsed.get("code"):
        ext = parsed.get("extension", default_ext)
        output_dir = await output_dir_task
        filename = get_unique_filename(output_dir, model_name, ext)
        filepath = os.path.join(output_dir, filename)

        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write(parsed["code"])

        # Dependency detection + venv setup for Python files
        venv_python = None
        if auto_install == "on" and ext == ".py" and auto_open != "off":
            try:
                packages = await _detect_dependencies(
                    session, api_key, parsed["code"])
                if packages:
                    venv_python = await _ensure_venv(output_dir)
                    await _install_packages(venv_python, packages)
            except Exception:
                venv_python = None

            # Fall back to existing venv if one was created by another model
            if venv_python is None:
                candidate = _venv_python_path(output_dir)
                if os.path.isfile(candidate):
                    venv_python = candidate

        if auto_open == "incremental":
            _open_file_in_tab(filepath, interp=venv_python)

        elapsed = time.monotonic() - start
        if not registered:
            print(f"  {_ok} {S.BOLD}{model_name:<{pad}}{S.RST}  "
                  f"saved {_arrow} {S.GRN}{filename}{S.RST}  "
                  f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "success", "time_s": elapsed, "file": filename,
            "usage": usage, "venv_python": venv_python}
    else:
        elapsed = time.monotonic() - start
        if not registered:
            print(f"  {_fail} {model_name:<{pad}}  "
                  f"{S.RED}parse failed{S.RST}  "
                  f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": usage}

async def process_model_text(session: aiohttp.ClientSession, api_key: str, model_name: str, model_id: str, prompt: str,
                             output_dir_task: asyncio.Task, semaphore: asyncio.Semaphore,
                             results: Dict[str, Any], pad: int, tracker: Any,
                             reasoning_effort: Optional[str] = "high",
                             auto_open: str = "off") -> None:
    """Query a single model for a text response and save as Markdown."""
    start = time.monotonic()
    registered = tracker.is_running

    if registered:
        tracker.register(model_name)
    else:
        print(f"  {_wait} {model_name:<{pad}}  "
              f"{S.DIM}calling {model_id}…{S.RST}")

    content: Optional[str] = None
    usage: dict = {}

    try:
        async with semaphore:
            def _on_progress(chars: int) -> None:
                if registered:
                    tracker.update(model_name, chars)

            content, usage = await call_model_streaming(
                session, api_key, model_id, prompt,
                reasoning_effort=reasoning_effort,
                on_progress=_on_progress)

    except asyncio.CancelledError:
        elapsed = time.monotonic() - start
        if not registered:
            print(f"  {_skip} {model_name:<{pad}}  "
                  f"{S.DIM}cancelled  [{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "cancelled", "time_s": elapsed, "file": None, "usage": {}}
        return
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        if not registered:
            print(f"  {_fail} {model_name:<{pad}}  "
                  f"{S.RED}timeout{S.RST}  "
                  f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return
    except aiohttp.ClientError as exc:
        elapsed = time.monotonic() - start
        if not registered:
            print(f"  {_fail} {model_name:<{pad}}  "
                  f"{S.RED}API error: {exc}{S.RST}  "
                  f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return
    except Exception as exc:
        elapsed = time.monotonic() - start
        exc_str = str(exc) or exc.__class__.__name__
        if not registered:
            print(f"  {_fail} {model_name:<{pad}}  "
                  f"{S.RED}{exc_str}{S.RST}  "
                  f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return
    finally:
        if registered:
            tracker.unregister(model_name)

    elapsed = time.monotonic() - start

    if not content:
        if not registered:
            print(f"  {_fail} {model_name:<{pad}}  "
                  f"{S.RED}no response{S.RST}  "
                  f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return

    output_dir = await output_dir_task
    filename = get_unique_filename(output_dir, model_name, ".md")
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write(content)

    if auto_open == "incremental":
        _open_file_in_tab(filepath)

    elapsed = time.monotonic() - start
    if not registered:
        print(f"  {_ok} {S.BOLD}{model_name:<{pad}}{S.RST}  "
              f"saved {_arrow} {S.GRN}{filename}{S.RST}  "
              f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
    results[model_name] = {
        "status": "success", "time_s": elapsed, "file": filename, "usage": usage}

async def main_async(args: Any, api_key: str, model_mapping: Optional[Dict[str, str]] = None,
                     config: Optional[Dict[str, Any]] = None,
                     pricing_lookup: Optional[Dict[str, Any]] = None) -> None:
    from wavebench.models import MODEL_MAPPING
    mapping = model_mapping if model_mapping is not None else MODEL_MAPPING
    pad = max((len(n) for n in mapping), default=12) + 1

    if config is None:
        from wavebench.storage import load_config
        config = load_config()

    raw_effort = config.get("reasoning_effort", "high")
    reasoning_effort: Optional[str] = None if raw_effort == "off" else raw_effort

    auto_open = config.get("auto_open", "off")
    if getattr(args, "auto_open", None):
        auto_open = args.auto_open

    auto_install = config.get("auto_install", "off")
    if getattr(args, "auto_install", None):
        auto_install = "on"

    _reset_incremental_tabs()

    user_prompt = args.prompt
    text_mode = getattr(args, "text", False)

    if text_mode:
        sys_prompt = SYSTEM_PROMPT_TEXT
        full_prompt = f"{sys_prompt}\n\nQuestion: {user_prompt}"
        default_ext = ".md"
    else:
        sys_prompt = (SYSTEM_PROMPT_CODE_DEPS if auto_install == "on"
                      else SYSTEM_PROMPT_CODE)
        full_prompt = f"{sys_prompt}\n\nTask: {user_prompt}"
        default_ext = (
            ".py" if "python" in user_prompt.lower()
            or ".py" in user_prompt.lower()
            else ".html"
        )

    targets = list(mapping.items())
    if not targets:
        print(f"  {_fail} No models configured in MODEL_MAPPING.")
        return

    mode_label = f"{S.HYEL}TEXT{S.RST}" if text_mode else f"{_styles.ACCENT_HI}CODE{S.RST}"

    w = _tw() - 4
    if reasoning_effort:
        reasoning_label = f"{S.HGRN}{reasoning_effort}{S.RST}"
    else:
        reasoning_label = f"{S.HRED}off{S.RST}"
    print()
    print(_box_top("", w, heavy=True))
    print(_box_row(
        f"{S.DIM}{'MODE':>8}{S.RST}  {mode_label}", w, heavy=True))
    print(_box_row(
        f"{S.DIM}{'PROMPT':>8}{S.RST}  "
        f"{S.BOLD}{_truncate(user_prompt, w - 16)}{S.RST}", w, heavy=True))
    print(_box_divider(w, heavy=True))
    print(_box_row(
        f"{S.DIM}{'MODELS':>8}{S.RST}  {len(targets)} active", w, heavy=True))
    print(_box_row(
        f"{S.DIM}{'REASON':>8}{S.RST}  {reasoning_label}", w, heavy=True))
    print(_box_bot(w, heavy=True))
    print()

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=0, keepalive_timeout=30)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    results: Dict[str, Any] = {}
    output_dir_final = [None]
    t0 = time.monotonic()

    history = load_history()
    avg_tokens: Dict[str, float] = {}
    for run in history.get("runs", []):
        for name, res in run.get("models", {}).items():
            if res.get("status") == "success":
                tkns = (res.get("usage") or {}).get("total_tokens")
                if tkns:
                    avg_tokens.setdefault(name, []).append(tkns)  # type: ignore[arg-type]
    avg_tokens = {k: sum(v) / len(v) for k, v in avg_tokens.items()}  # type: ignore[arg-type]

    async with aiohttp.ClientSession(
        timeout=timeout, connector=connector,
    ) as session:
        model_names = [name for name, _ in targets]
        model_id_map = {name: mid for name, mid in targets}
        tracker = ProgressTracker(
            len(targets), results, pad=pad, model_names=model_names,
            avg_tokens=avg_tokens,
            pricing_lookup=pricing_lookup or {},
            model_id_map=model_id_map,
            alt_screen=True)
        try:
            async def resolve_output_dir() -> str:
                dir_name = await get_directory_name(
                    session, api_key, user_prompt)
                
                base_out = os.path.join(os.getcwd(), OUTPUT_DIR)
                out = os.path.join(base_out, dir_name)
                os.makedirs(out, exist_ok=True)

                pf = os.path.join(out, "prompt.txt")
                if not os.path.exists(pf):
                    with open(pf, "w", encoding="utf-8") as fh:
                        fh.write(user_prompt)

                output_dir_final[0] = out
                tracker.set_output_dir(out)
                return out

            output_dir_task = asyncio.create_task(resolve_output_dir())
            await tracker.start()

            if text_mode:
                tasks = [
                    process_model_text(
                        session, api_key, name, mid, full_prompt,
                        output_dir_task, semaphore, results, pad,
                        tracker, reasoning_effort=reasoning_effort,
                        auto_open=auto_open,
                    )
                    for name, mid in targets
                ]
            else:
                tasks = [
                    process_model(
                        session, api_key, name, mid, full_prompt,
                        default_ext, output_dir_task, semaphore, results, pad,
                        tracker, reasoning_effort=reasoning_effort,
                        auto_open=auto_open,
                        auto_install=auto_install,
                    )
                    for name, mid in targets
                ]
            await asyncio.gather(*tasks, return_exceptions=True)

            if auto_open == "after_all" and output_dir_task.done():
                out = output_dir_task.result()
                code_tabs: List[tuple] = []
                for name, info in results.items():
                    if info.get("status") != "success" or not info.get("file"):
                        continue
                    fp = os.path.join(out, info["file"])
                    ext = os.path.splitext(fp)[1].lower()
                    if ext in _INTERPRETER_MAP:
                        vp = info.get("venv_python")
                        if not vp and ext == ".py":
                            candidate = _venv_python_path(out)
                            if os.path.isfile(candidate):
                                vp = candidate
                        interp = vp or _resolve_interpreter(fp, ext)
                        if interp:
                            code_tabs.append((fp, interp))
                        else:
                            _open_with_viewer(fp)
                    else:
                        _open_with_viewer(fp)
                if code_tabs:
                    _open_files_as_tabs(code_tabs)

            if not output_dir_task.done():
                output_dir_task.cancel()

        except asyncio.CancelledError:
            print(f"\n  {S.DIM}Cancelled.{S.RST}")
        except Exception as exc:
            exc_str = str(exc) or exc.__class__.__name__
            print(f"\n  {_fail} {S.RED}{exc_str}{S.RST}")
        finally:
            await tracker.stop()

    # ── Run results ────────────────────────────────────────────────────────
    total_time = time.monotonic() - t0
    _pricing = pricing_lookup or {}
    _id_map = {name: mid for name, mid in targets}

    def _result_cost(name: str, info: Dict[str, Any]) -> Optional[float]:
        mid = _id_map.get(name, "")
        return compute_cost(info.get("usage", {}), _pricing.get(mid, {}))

    if not tracker.rendered_final:
        ok   = sum(1 for v in results.values() if v["status"] == "success")
        fail = sum(1 for v in results.values() if v["status"] == "failed")
        canc = sum(1 for v in results.values() if v["status"] == "cancelled")
        inner_w = w - 4

        print()
        print(_box_top("Run Results", w))
        if output_dir_final[0]:
            out_path = output_dir_final[0]
            max_path = inner_w - 10
            if len(out_path) > max_path:
                out_path = "…" + out_path[-(max_path - 1):]
            print(_box_row(
                f"{S.DIM}{'OUTPUT':>8}  {out_path}{S.RST}", w))
            print(_box_sep("", w))
        else:
            print(_box_row("", w))

        def _rank_key(item: Any) -> Any:
            _, v = item
            order = {"success": 0, "failed": 1, "cancelled": 2}
            return (order.get(v["status"], 3), v["time_s"])

        _total_run_cost = 0.0
        _has_any_cost = False

        for i, (name, info) in enumerate(
            sorted(results.items(), key=_rank_key), 1
        ):
            st = info["status"]
            t = format_duration(info["time_s"])
            model_cost = _result_cost(name, info)
            if model_cost is not None:
                _total_run_cost += model_cost
                _has_any_cost = True
            cost_s = (f"  {S.HYEL}{format_cost(model_cost)}{S.RST}"
                      if model_cost else "")
            if st == "success":
                sym = _ok
                usage_d = info.get("usage", {})
                tokens = usage_d.get("total_tokens")
                fname = info['file']
                tk_part = f"  {S.DIM}{tokens:,} tk{S.RST}" if tokens else ""
                detail = f"saved {_arrow} {S.GRN}{fname}{S.RST}{tk_part}{cost_s}"
            elif st == "cancelled":
                sym = _skip
                detail = f"{S.DIM}cancelled{S.RST}"
            else:
                sym = _fail
                detail = f"{S.RED}failed{S.RST}{cost_s}"
            rank = f"{S.DIM}{i:>2}.{S.RST}"
            content = f"{rank} {sym} {_rpad(name, pad)}  {detail}"
            if st == "success" and _vlen(content) + 2 + len(t) > inner_w:
                overflow = _vlen(content) + 2 + len(t) - inner_w
                max_fname = max(8, len(fname) - overflow)
                fname = _truncate(fname, max_fname)
                detail = f"saved {_arrow} {S.GRN}{fname}{S.RST}{tk_part}{cost_s}"
                content = f"{rank} {sym} {_rpad(name, pad)}  {detail}"
            gap = max(inner_w - _vlen(content) - len(t), 2)
            print(_box_row(
                f"{content}{' ' * gap}{S.DIM}{t}{S.RST}", w))

        print(_box_sep("", w))
        parts: list[str] = []
        if ok:   parts.append(f"{S.HGRN}{ok} passed{S.RST}")
        if fail: parts.append(f"{S.HRED}{fail} failed{S.RST}")
        if canc: parts.append(f"{S.DIM}{canc} cancelled{S.RST}")
        parts.append(f"{format_duration(total_time)} total")
        if _has_any_cost:
            parts.append(f"{S.HYEL}{format_cost(_total_run_cost)}{S.RST}")
        sep = f" {_dot} "
        print(_box_row(sep.join(parts), w))
        print(_box_bot(w))

    # ── Record run & show lifetime analytics ───────────────────────────────
    run_costs = {name: _result_cost(name, info) for name, info in results.items()}
    record_run(history, user_prompt, output_dir_final[0],
               total_time, results, costs=run_costs)
    display_analytics(history, compact=True, pad=pad,
                      sort_by=config.get("analytics_sort", "runs"))
    print()
