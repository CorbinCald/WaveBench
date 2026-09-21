"""Small setup screen for Harness subagents, shared by startup and configuration."""

from __future__ import annotations

import shutil
import sys

from wavebench.harness.config import Limits
from wavebench.harness.subagents import subagents_status
from wavebench.tui.input import _read_key_or_resize, hold_raw
from wavebench.tui.styles import S, _box_bot, _box_row, _box_top, _truncate

PARALLEL_RANGE = (2, 5)
MAX_CAP = 9_999
FIELDS = ("enabled", "parallel", "cap")


def interactive_subagents(config: dict, *, alternate_screen: bool = True) -> dict | None:
    """Return an updated config with subagents and harness caps, or None on cancel."""
    if not sys.stdin.isatty():
        print("Subagent setup requires a terminal. Use --subagents and --agent-cap instead.")
        return None
    defaults = Limits()
    harness = dict(config.get("harness") or {})
    enabled = config.get("subagents", "off") == "on"
    parallel = harness.get("subagent_parallel", defaults.subagent_parallel)
    if not isinstance(parallel, int) or not PARALLEL_RANGE[0] <= parallel <= PARALLEL_RANGE[1]:
        parallel = defaults.subagent_parallel
    cap_buffer = str(harness.get("subagent_cap", defaults.subagent_cap))
    cursor = 0
    message = ""

    def cap_value() -> int | None:
        try:
            value = int(cap_buffer)
        except ValueError:
            return None
        return value if 1 <= value <= MAX_CAP else None

    def updated() -> dict:
        return {
            **config,
            "subagents": "on" if enabled else "off",
            "harness": {**harness, "subagent_parallel": parallel, "subagent_cap": cap_value()},
        }

    def render() -> None:
        width = max(20, min(100, shutil.get_terminal_size((80, 24)).columns) - 4)
        state = subagents_status(updated()) if cap_value() else "On (cap needed)"
        rows = [
            f"Subagents: {state} · Harness agents",
            "Each model may delegate self-contained tasks to parallel subagents of",
            "the same model. They share its workspace, tools, token budget, and",
            "phase time; only their reports and changed files return to the lead.",
            "",
        ]
        fields = [
            ("Enabled", "on" if enabled else "off", "Space toggles"),
            (
                "Parallel agents",
                str(parallel),
                f"{PARALLEL_RANGE[0]}-{PARALLEL_RANGE[1]} at once · Space or ←→",
            ),
            (
                "Total agent cap",
                cap_buffer or "(empty)",
                "per model, build + repair · digits or ←→",
            ),
        ]
        for index, (label, value, hint) in enumerate(fields):
            marker = "▸" if index == cursor else " "
            rows.append(f"{marker} {label:<16} {value:<6} {S.DIM}{hint}{S.RST}")
        rows.extend(["", "↑↓ select · Enter save · Esc cancel", message])
        frame = [_box_top("Subagents", width)]
        frame.extend(_box_row(_truncate(row, width - 4), width) for row in rows)
        frame.append(_box_bot(width))
        sys.stdout.write("\033[2J\033[H" + "\r\n".join(frame))
        sys.stdout.flush()

    if alternate_screen:
        sys.stdout.write("\033[?1049h")
    sys.stdout.write("\033[?25l")
    try:
        with hold_raw():
            while True:
                render()
                key = _read_key_or_resize()
                if key in ("escape", "ctrl-c"):
                    return None
                if key == "enter":
                    if cap_value() is None:
                        message = f"Enter a total agent cap from 1 to {MAX_CAP:,}."
                        continue
                    return updated()
                message = ""
                field = FIELDS[cursor]
                if key in ("up", "down"):
                    cursor = (cursor + (1 if key == "down" else -1)) % len(FIELDS)
                elif field == "enabled" and key in ("space", "left", "right"):
                    enabled = not enabled
                elif field == "parallel" and key in ("space", "left", "right"):
                    low, high = PARALLEL_RANGE
                    step = -1 if key == "left" else 1
                    parallel = low + (parallel - low + step) % (high - low + 1)
                elif field == "cap":
                    if key in ("left", "right"):
                        current = cap_value() or 1
                        step = 1 if key == "right" else -1
                        cap_buffer = str(max(1, min(MAX_CAP, current + step)))
                    elif key == "backspace":
                        cap_buffer = cap_buffer[:-1]
                    elif key == "ctrl-a":
                        cap_buffer = ""
                    elif len(key) == 1 and key in "0123456789" and len(cap_buffer) < 4:
                        cap_buffer += key
    finally:
        sys.stdout.write(S.RST + "\033[?25h")
        if alternate_screen:
            sys.stdout.write("\033[?1049l")
        sys.stdout.flush()
