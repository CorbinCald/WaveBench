"""Small, masked Brave setup screen shared by startup and configuration."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import webbrowser

from wavebench.tui.input import _read_key_or_resize, hold_raw
from wavebench.tui.styles import S, _box_bot, _box_row, _box_top, _truncate
from wavebench.web_search import (
    BRAVE_DASHBOARD,
    SearchError,
    load_brave_key,
    save_brave_key,
    search_status,
    validate_brave_key,
)


def interactive_web_search(config: dict, *, alternate_screen: bool = True) -> dict | None:
    if not sys.stdin.isatty():
        print("Brave setup requires a terminal. Set BRAVE_SEARCH_API_KEY and use --web-search.")
        return None
    existing_key = load_brave_key()
    environment_key = bool(os.environ.get("BRAVE_SEARCH_API_KEY", "").strip())
    editing = not existing_key
    buffer = ""
    message = ""

    def render():
        width = max(20, min(100, shutil.get_terminal_size((80, 24)).columns) - 4)
        rows = [
            f"Web search: {search_status(config)} · Harness agents",
            "1. Create a Brave account and choose a Search API plan.",
            "2. Create an API key at the dashboard below.",
            BRAVE_DASHBOARD,
            "3. Paste the key, then Enter to test and enable.",
            "Testing sends one search; Brave charges apply separately.",
            "",
        ]
        if editing:
            rows.extend(
                [
                    "API key: " + ("*" * min(len(buffer), 24) or "(paste here)"),
                    "Enter test & enable · Ctrl-A clear · Esc cancel",
                ]
            )
        else:
            source = "BRAVE_SEARCH_API_KEY" if environment_key else "private local file"
            rows.extend([f"Key configured via {source}.", "Enter test & enable · K replace key"])
        rows.extend(
            [
                "Ctrl-D disable · Ctrl-O dashboard · Esc cancel"
                if editing
                else "D disable · O open dashboard · Esc cancel",
                message,
            ]
        )
        frame = [_box_top("Brave web search", width)]
        frame.extend(_box_row(_truncate(row, width - 4), width) for row in rows)
        frame.append(_box_bot(width))
        sys.stdout.write("\033[2J\033[H" + "\r\n".join(frame))
        sys.stdout.flush()

    if alternate_screen:
        sys.stdout.write("\033[?1049h")
    sys.stdout.write("\033[?25l")
    try:
        # Keep ECHO off even while rendering and validating a pasted secret.
        with hold_raw():
            while True:
                render()
                key = _read_key_or_resize()
                if key in ("escape", "ctrl-c"):
                    return None
                if key == "enter":
                    candidate = buffer.strip() if editing else existing_key
                    if not candidate:
                        message = "Paste a Brave API key first."
                        continue
                    message = "Testing Brave connection…"
                    render()
                    try:
                        asyncio.run(validate_brave_key(candidate))
                        if not environment_key:
                            save_brave_key(candidate)
                    except SearchError as exc:
                        message = str(exc)
                        continue
                    except (OSError, ValueError):
                        message = (
                            "Could not save the key. Check .benchmark_secrets.json permissions."
                        )
                        continue
                    return {**config, "web_search": "on"}
                if key == "\x0f" or (key.lower() == "o" and not editing):
                    webbrowser.open(BRAVE_DASHBOARD)
                elif key == "\x04" or (key.lower() == "d" and not editing):
                    return {**config, "web_search": "off"}
                elif not editing and key.lower() == "k":
                    if environment_key:
                        message = "Update or unset BRAVE_SEARCH_API_KEY to replace this key."
                    else:
                        editing, buffer, message = True, "", ""
                elif editing and key == "backspace":
                    buffer = buffer[:-1]
                elif editing and key == "ctrl-a":
                    buffer = ""
                elif (
                    editing
                    and len(key) == 1
                    and key.isascii()
                    and key.isprintable()
                    and len(buffer) < 512
                ):
                    buffer += key
    finally:
        sys.stdout.write(S.RST + "\033[?25h")
        if alternate_screen:
            sys.stdout.write("\033[?1049l")
        sys.stdout.flush()
