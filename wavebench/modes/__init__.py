"""Response modes — first-class abstractions over Code / Text / TTS variants.

A ``Mode`` captures two mode-specific decisions that used to be spread
across ``process_model``/``process_model_text``:

  - **Prompt framing** — how to wrap the user's request with a system
    prompt and any mode-specific instructions.
  - **Response parsing** — how to convert a raw LLM response into a
    :class:`ParsedOutput` (content, extension, pass/fail).

Concrete modes live in ``code.py`` (``CodeMode``), ``text.py``
(``TextMode``), and ``tts.py`` (``TTSMode``). The ``MODES`` registry is
populated on import and keyed by each mode's ``name`` for CLI lookup
(``--mode code``, ``--mode text``, ``--mode tts``).

## Adding a new mode

1. Create ``wavebench/modes/your_mode.py`` defining a class with
   ``name``, ``display_name``, ``frame_prompt``, and ``parse_response``.
2. Instantiate a singleton, import it in this ``__init__.py``, and call
   ``register(YOUR_MODE)``.
3. Add unit tests in ``tests/unit/test_modes.py``.

The ``Mode`` type is a :class:`typing.Protocol` — structural, not an ABC,
so implementations don't need to inherit anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ParsedOutput:
    """Result of converting a raw LLM response into a savable file payload.

    Attributes:
        content: Text or bytes to write to the output file (including any
            trailing newline the mode wants to enforce).
        extension: File extension without the leading dot — e.g., ``"py"``,
            ``"md"``, ``"html"``. Empty string means "unknown".
        parse_ok: True if the mode was able to recognize meaningful output,
            False if the response was empty or malformed. Drives the
            leaderboard's pass/fail classification.
        parse_error: Human-readable reason when ``parse_ok`` is False;
            ``None`` otherwise.
    """

    content: str | bytes
    extension: str
    parse_ok: bool
    parse_error: str | None = None


class Mode(Protocol):
    """Structural interface for a response mode.

    Implementations are typically frozen dataclasses so they act as value
    objects. The registry holds one canonical instance per ``name``;
    callers can construct variants (e.g., ``CodeMode(allow_deps=True)``)
    for a specific run without re-registering.
    """

    name: str
    display_name: str

    def frame_prompt(self, user_prompt: str) -> str:
        """Wrap the raw user prompt with mode-specific instructions.

        Returns a single string that the OpenRouter client will send as
        the ``user`` message content for chat modes or as ``input`` for
        TTS mode. (The spec envisioned an OpenAI-style messages list; the
        current chat client accepts only a string prompt, so we match that
        — upgrading is a separate concern.)
        """
        ...

    def parse_response(self, raw: str | bytes) -> ParsedOutput:
        """Turn a raw model response into a :class:`ParsedOutput`."""
        ...


MODES: dict[str, Mode] = {}


def register(mode: Mode) -> None:
    """Add *mode* to the global registry, keyed by ``mode.name``."""
    MODES[mode.name] = mode


# ── Built-in modes ───────────────────────────────────────────────────────
from .code import CODE_MODE  # noqa: E402  (avoid circular import)
from .text import TEXT_MODE  # noqa: E402
from .tts import TTS_MODE  # noqa: E402

register(CODE_MODE)
register(TEXT_MODE)
register(TTS_MODE)

__all__ = [
    "CODE_MODE",
    "MODES",
    "TEXT_MODE",
    "TTS_MODE",
    "Mode",
    "ParsedOutput",
    "register",
]
