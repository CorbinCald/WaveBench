"""Per-model benchmark runner and the filename utility it shares.

``run_model`` is the single mode-parameterized driver that streams an
LLM response, parses it via the supplied :class:`~wavebench.modes.Mode`,
writes the output file, and (for code mode) optionally detects Python
dependencies and installs them into a shared per-output-dir venv.

Previously this module held a ``process_model`` / ``process_model_text``
pair — near-duplicate ~150-line siblings split only on text vs code. The
``Mode`` abstraction collapses them; the mode supplies its own prompt
framing and response parser, and all other logic is shared.

``get_unique_filename`` lives here because it's used only by this runner,
but is re-exported from ``core/__init__.py`` for callers that want it.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

import aiohttp

from wavebench.api import call_model_streaming
from wavebench.modes import Mode
from wavebench.tui.styles import (
    S,
    _arrow,
    _fail,
    _ok,
    _skip,
    _tri,
    _wait,
    _work,
    format_duration,
)

from .auto_install import (
    _detect_dependencies,
    _ensure_venv,
    _install_packages,
    _venv_python_path,
)
from .auto_open import _open_file_in_tab


def get_unique_filename(directory: str, base_name: str, extension: str) -> str:
    """Return a unique filename, appending _v2, _v3, … if needed."""
    base = re.sub(r"cursor", "", base_name, flags=re.IGNORECASE)
    if not extension.startswith("."):
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


async def run_model(
    mode: Mode,
    session: aiohttp.ClientSession,
    api_key: str,
    model_name: str,
    model_id: str,
    user_prompt: str,
    default_ext: str,
    output_dir_task: asyncio.Task,
    semaphore: asyncio.Semaphore,
    results: dict[str, Any],
    pad: int,
    tracker: Any,
    reasoning_effort: str | None = "high",
    auto_open: str = "off",
    auto_install: str = "off",
) -> None:
    """Execute one model against *mode*; write output; record result.

    ``mode.frame_prompt`` wraps *user_prompt* with mode-specific
    instructions; ``mode.parse_response`` converts the raw streamed
    response into a :class:`~wavebench.modes.ParsedOutput` whose
    ``extension`` drives the saved file's suffix.

    When ``mode.name == "code"`` AND ``auto_install == "on"`` AND the
    parsed extension is ``py``, dependency detection + venv setup fire
    as before. Text mode skips that branch entirely.
    """
    start = time.monotonic()
    registered = tracker.is_running

    if registered:
        tracker.register(model_name)
    else:
        print(f"  {_wait} {model_name:<{pad}}  {S.DIM}calling {model_id}…{S.RST}")

    framed_prompt = mode.frame_prompt(user_prompt)
    content: str | None = None
    usage: dict = {}
    retry_events: list[dict[str, Any]] = []

    try:
        async with semaphore:

            def _on_progress(chars: int) -> None:
                if registered:
                    tracker.update(model_name, chars)

            def _on_retry(status: int, attempt: int, max_attempts: int, wait_s: float) -> None:
                retry_events.append(
                    {"status": status, "attempt": attempt, "wait_s": round(wait_s, 2)}
                )
                if registered:
                    tracker.note_retry(model_name, status, attempt, max_attempts, wait_s)
                else:
                    print(
                        f"    {_tri} {model_name:<{pad}}  "
                        f"{S.YEL}HTTP {status}{S.RST} "
                        f"{S.DIM}retry {attempt}/{max_attempts} in {wait_s:.1f}s{S.RST}"
                    )

            content, usage = await call_model_streaming(
                session,
                api_key,
                model_id,
                framed_prompt,
                reasoning_effort=reasoning_effort,
                on_progress=_on_progress,
                on_retry=_on_retry,
            )

    except asyncio.CancelledError:
        elapsed = time.monotonic() - start
        if not registered:
            print(
                f"  {_skip} {model_name:<{pad}}  "
                f"{S.DIM}cancelled  [{format_duration(elapsed)}]{S.RST}"
            )
        results[model_name] = {
            "status": "cancelled",
            "time_s": elapsed,
            "file": None,
            "usage": {},
            "retries": retry_events,
        }
        return
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        if not registered:
            print(
                f"  {_fail} {model_name:<{pad}}  "
                f"{S.RED}timeout{S.RST}  "
                f"{S.DIM}[{format_duration(elapsed)}]{S.RST}"
            )
        results[model_name] = {
            "status": "failed",
            "time_s": elapsed,
            "file": None,
            "usage": {},
            "retries": retry_events,
        }
        return
    except aiohttp.ClientError as exc:
        elapsed = time.monotonic() - start
        if not registered:
            print(
                f"  {_fail} {model_name:<{pad}}  "
                f"{S.RED}API error: {exc}{S.RST}  "
                f"{S.DIM}[{format_duration(elapsed)}]{S.RST}"
            )
        results[model_name] = {
            "status": "failed",
            "time_s": elapsed,
            "file": None,
            "usage": {},
            "retries": retry_events,
        }
        return
    except Exception as exc:
        elapsed = time.monotonic() - start
        exc_str = str(exc) or exc.__class__.__name__
        if not registered:
            print(
                f"  {_fail} {model_name:<{pad}}  "
                f"{S.RED}{exc_str}{S.RST}  "
                f"{S.DIM}[{format_duration(elapsed)}]{S.RST}"
            )
        results[model_name] = {
            "status": "failed",
            "time_s": elapsed,
            "file": None,
            "usage": {},
            "retries": retry_events,
        }
        return
    finally:
        if registered:
            tracker.unregister(model_name)

    elapsed = time.monotonic() - start

    if not content:
        if not registered:
            print(
                f"  {_fail} {model_name:<{pad}}  "
                f"{S.RED}no response{S.RST}  "
                f"{S.DIM}[{format_duration(elapsed)}]{S.RST}"
            )
        results[model_name] = {
            "status": "failed",
            "time_s": elapsed,
            "file": None,
            "usage": {},
            "retries": retry_events,
        }
        return

    if registered:
        tracker.mark_parsing(model_name)
    else:
        action = "parsing" if mode.name == "code" else "saving"
        print(f"  {_work} {model_name:<{pad}}  {S.DIM}{action}…{S.RST}")

    try:
        parsed = mode.parse_response(content)

        if not parsed.parse_ok:
            elapsed = time.monotonic() - start
            if not registered:
                print(
                    f"  {_fail} {model_name:<{pad}}  "
                    f"{S.RED}parse failed{S.RST}  "
                    f"{S.DIM}[{format_duration(elapsed)}]{S.RST}"
                )
            results[model_name] = {
                "status": "failed",
                "time_s": elapsed,
                "file": None,
                "usage": usage,
                "retries": retry_events,
            }
            return

        ext = parsed.extension or default_ext.lstrip(".")
        output_dir = await output_dir_task
        filename = get_unique_filename(output_dir, model_name, ext)
        filepath = os.path.join(output_dir, filename)

        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write(parsed.content)

        # Code-mode dependency auto-install lives here rather than inside
        # the Mode itself so orchestrator-level config (auto_install,
        # auto_open) can gate it uniformly.
        venv_python = None
        if mode.name == "code" and auto_install == "on" and ext == "py" and auto_open != "off":
            try:
                packages = await asyncio.wait_for(
                    _detect_dependencies(session, api_key, parsed.content), timeout=15.0
                )
                if packages:
                    venv_python = await _ensure_venv(output_dir)
                    await asyncio.wait_for(_install_packages(venv_python, packages), timeout=120.0)
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
            print(
                f"  {_ok} {S.BOLD}{model_name:<{pad}}{S.RST}  "
                f"saved {_arrow} {S.GRN}{filename}{S.RST}  "
                f"{S.DIM}[{format_duration(elapsed)}]{S.RST}"
            )
        result: dict[str, Any] = {
            "status": "success",
            "time_s": elapsed,
            "file": filename,
            "usage": usage,
            "retries": retry_events,
        }
        if mode.name == "code":
            result["venv_python"] = venv_python
        results[model_name] = result
    finally:
        if registered:
            tracker.finish_parsing(model_name)
