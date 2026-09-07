"""Portable prompt history with read-only import of legacy readline files.

GNU Readline stores plain lines; libedit stores BSD vis-escaped bytes under a
_HiStOrY_V2_ header. Neither backend is needed by our own prompt editor. New
history uses versioned UTF-8 JSON and leaves legacy files untouched.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path

HISTORY_LIMIT = 500
_LIBEDIT_HEADER = b"_HiStOrY_V2_"
_VIS_ESCAPE = re.compile(rb"\\([0-3][0-7]{2}|\^[\x00-\x7f]|M[-^][\x00-\x7f]|\\)")


def _decode_libedit(line: bytes) -> str:
    """Decode libedit's VIS_WHITE format once, without changing literal escapes.

    In particular, a saved literal '\\040' is '\\134040', not a space. Decode
    bytes before UTF-8 so both literal Unicode and vis-escaped UTF-8 work.
    """

    def replace(match: re.Match[bytes]) -> bytes:
        value = match[1]
        if value[:1] in b"0123":
            return bytes([int(value, 8)])
        if value == b"\\":
            return b"\\"
        meta = value.startswith(b"M")
        prefix, char = value[-2:]
        if prefix == ord("^"):
            char = 127 if char == ord("?") else char & 31
        return bytes([char | (128 if meta else 0)])

    return _VIS_ESCAPE.sub(replace, line).decode("utf-8")


def _read(path: str) -> list[str]:
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return []
    if path.endswith(".json"):
        payload = json.loads(data)
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or not isinstance(payload.get("entries"), list)
            or not all(isinstance(entry, str) for entry in payload["entries"])
        ):
            raise ValueError("unrecognized prompt history format")
        entries = payload["entries"]
    else:
        lines = data.split(b"\n")
        libedit = lines[0].removesuffix(b"\r") == _LIBEDIT_HEADER
        if libedit:
            lines = lines[1:]
        entries = [
            _decode_libedit(line.removesuffix(b"\r"))
            if libedit
            else line.removesuffix(b"\r").decode("utf-8")
            for line in lines
            if line
        ]
    return [entry for entry in entries if entry][-HISTORY_LIMIT:]


def load(path: str) -> list[str]:
    try:
        return _read(path)
    except (OSError, UnicodeError, ValueError):
        return []


def save(path: str, query: str, *, source: str) -> bool:
    """Append to the latest saved history; preserve originals if reading fails."""
    if not query:
        return False
    temp = None
    try:
        # Re-read on save, rather than writing a stale list from prompt startup.
        entries = [*_read(path if os.path.exists(path) else source), query][-HISTORY_LIMIT:]
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=Path(path).parent,
            prefix=Path(path).name + ".",
            suffix=".tmp",
            delete=False,
        ) as file:
            temp = file.name
            json.dump({"version": 1, "entries": entries}, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
        return True
    except (OSError, UnicodeError, ValueError):
        return False
    finally:
        if temp is not None:
            with suppress(OSError):
                os.unlink(temp)
