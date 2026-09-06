"""Harness mode; CodeMode and its parser remain import-compatible for history."""

from __future__ import annotations

from dataclasses import dataclass

from wavebench.modes import ParsedOutput
from wavebench.parsers import extract_code


@dataclass(frozen=True)
class CodeMode:
    """Multi-file projects, with an optional isolated requirements.txt policy."""

    name: str = "harness"
    display_name: str = "Harness"
    allow_deps: bool = False

    def frame_prompt(self, user_prompt: str) -> str:
        from wavebench.harness.session import system_prompt

        return f"{system_prompt('on' if self.allow_deps else 'off')}\n\nTask: {user_prompt}"

    def parse_response(self, raw: str | bytes) -> ParsedOutput:
        """Read historical one-shot artifacts; harness generation never calls this."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        parsed = extract_code(raw)
        if not parsed or not parsed.get("code"):
            return ParsedOutput(
                content="",
                extension="",
                parse_ok=False,
                parse_error="code extraction failed — no recognizable code block",
            )
        ext = parsed.get("extension", "") or ""
        if ext.startswith("."):
            ext = ext[1:]
        return ParsedOutput(
            content=parsed["code"],
            extension=ext,
            parse_ok=True,
        )


CODE_MODE = CodeMode()
HARNESS_MODE = CODE_MODE
HarnessMode = CodeMode
