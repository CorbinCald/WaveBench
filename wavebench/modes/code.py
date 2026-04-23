"""Code mode — produces single-file source code from a user request.

Two framings are supported:
  - default (``allow_deps=False``) — tells the LLM to produce a
    dependency-free single-file implementation.
  - ``allow_deps=True`` — allows third-party PyPI packages, paired with
    the auto-install subsystem in ``core.auto_install``.

Response parsing delegates to :func:`wavebench.parsers.extract_code`,
which runs the four-stage JSON → fenced → salvage → fallback cascade.
"""

from __future__ import annotations

from dataclasses import dataclass

from wavebench.modes import ParsedOutput
from wavebench.parsers import extract_code

_SYSTEM_PROMPT_CODE = (
    "You are an expert programmer. Your goal is to provide a complete, "
    "fully functional, single-file implementation based on the user's request. "
    "Do not include any external modules or dependencies. "
    "Return ONLY the code, with no preamble or explanation."
)

_SYSTEM_PROMPT_CODE_DEPS = (
    "You are an expert programmer. Your goal is to provide a complete, "
    "fully functional, single-file implementation based on the user's request. "
    "You may use third-party packages from PyPI if they are helpful. "
    "Return ONLY the code, with no preamble or explanation."
)


@dataclass(frozen=True)
class CodeMode:
    """Code-generation mode.

    ``allow_deps`` swaps the system prompt so the model is permitted to
    use third-party packages. Two orchestrator-level instances coexist at
    runtime: the canonical ``CODE_MODE`` (no deps) in the registry, and a
    per-run ``CodeMode(allow_deps=True)`` when ``auto_install`` is on.
    """

    name: str = "code"
    display_name: str = "Code"
    allow_deps: bool = False

    def frame_prompt(self, user_prompt: str) -> str:
        sys_prompt = _SYSTEM_PROMPT_CODE_DEPS if self.allow_deps else _SYSTEM_PROMPT_CODE
        return f"{sys_prompt}\n\nTask: {user_prompt}"

    def parse_response(self, raw: str) -> ParsedOutput:
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
