"""Text mode — prose answers saved as Markdown, no parsing.

The system prompt nudges the model toward clear, markdown-formatted
prose. ``parse_response`` is a pass-through: the raw response is saved
verbatim with a ``.md`` extension.
"""

from __future__ import annotations

from dataclasses import dataclass

from wavebench.modes import ParsedOutput

_SYSTEM_PROMPT_TEXT = (
    "You are a knowledgeable assistant. Provide a clear, detailed, and "
    "well-structured answer to the user's question. Use Markdown formatting "
    "for readability. Do not include code unless the user explicitly asks for it."
)


@dataclass(frozen=True)
class TextMode:
    """Prose-response mode with markdown output."""

    name: str = "text"
    display_name: str = "Text"

    def frame_prompt(self, user_prompt: str) -> str:
        return f"{_SYSTEM_PROMPT_TEXT}\n\nQuestion: {user_prompt}"

    def parse_response(self, raw: str) -> ParsedOutput:
        if not raw or not raw.strip():
            return ParsedOutput(
                content="",
                extension="md",
                parse_ok=False,
                parse_error="empty response",
            )
        return ParsedOutput(
            content=raw,
            extension="md",
            parse_ok=True,
        )


TEXT_MODE = TextMode()
