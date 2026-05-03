"""TTS mode — synthesizes speech audio from the user's text.

Unlike Code/Text mode, TTS mode does not ask a chat model to transform the
prompt. The framed prompt is the exact text to synthesize, and parsing simply
validates the raw audio bytes returned by OpenRouter's `/audio/speech` endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass

from wavebench.modes import ParsedOutput


@dataclass(frozen=True)
class TTSMode:
    """Text-to-speech mode using OpenRouter's audio speech endpoint."""

    name: str = "tts"
    display_name: str = "TTS"
    voice: str = "alloy"
    response_format: str = "mp3"
    speed: float = 1.0

    def frame_prompt(self, user_prompt: str) -> str:
        return user_prompt.strip()

    def parse_response(self, raw: str | bytes) -> ParsedOutput:
        if not isinstance(raw, bytes) or not raw:
            return ParsedOutput(
                content=b"",
                extension=self.response_format,
                parse_ok=False,
                parse_error="empty audio response",
            )
        return ParsedOutput(
            content=raw,
            extension=self.response_format,
            parse_ok=True,
        )


TTS_MODE = TTSMode()
