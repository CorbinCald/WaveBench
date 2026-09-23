"""Scripted model tool calls for Harness tests.

Tests describe file work compactly as {"command": "write", "path": ..., "content": ...};
native() turns that into the model-facing tool name and arguments.
"""

from __future__ import annotations

NAMES = {
    "ls": "list_files",
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "delete": "delete_file",
    "lint": "lint",
    "done": "submit",
    "submit": "submit",
}
RENAMED = {"old": "old_text", "new": "new_text", "start": "start_line", "end": "end_line"}


def native(command: dict) -> tuple[str, dict]:
    """(tool name, arguments) for a compact command; other tools pass through unchanged."""
    command = dict(command)
    verb = command.pop("command", None)
    if verb not in NAMES:
        raise ValueError(f"unknown scripted command {verb!r}")
    arguments = {RENAMED.get(key, key): value for key, value in command.items()}
    arguments.pop("recursive", None)
    return NAMES[verb], arguments


def tool_call(call_id: str, command: dict, index: int = 0) -> dict:
    """A complete streamed tool call for one compact command."""
    import json

    name, arguments = native(command)
    return {
        "index": index,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
