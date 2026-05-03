"""Unit tests for ``wavebench.modes``.

Covers:
  - ``Mode`` protocol registration (both built-ins present).
  - ``CodeMode`` prompt framing (default vs allow_deps) and response
    parsing across the extraction cascade + failure path.
  - ``TextMode`` prompt framing and pass-through response parsing.
  - ``ParsedOutput`` dataclass shape and frozenness.
"""

from __future__ import annotations

import pytest

from wavebench.modes import CODE_MODE, MODES, TEXT_MODE, TTS_MODE, ParsedOutput
from wavebench.modes.code import CodeMode
from wavebench.modes.text import TextMode
from wavebench.modes.tts import TTSMode

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_both_builtins() -> None:
    assert set(MODES.keys()) == {"code", "text", "tts"}


def test_registry_maps_name_to_canonical_instance() -> None:
    assert MODES["code"] is CODE_MODE
    assert MODES["text"] is TEXT_MODE
    assert MODES["tts"] is TTS_MODE


def test_code_mode_defaults_disallow_deps() -> None:
    assert CODE_MODE.allow_deps is False


def test_mode_identity_attrs() -> None:
    assert CODE_MODE.name == "code"
    assert CODE_MODE.display_name == "Code"
    assert TEXT_MODE.name == "text"
    assert TEXT_MODE.display_name == "Text"
    assert TTS_MODE.name == "tts"
    assert TTS_MODE.display_name == "TTS"


# ---------------------------------------------------------------------------
# ParsedOutput
# ---------------------------------------------------------------------------


def test_parsed_output_is_frozen() -> None:
    # ``dataclasses.FrozenInstanceError`` subclasses ``AttributeError``
    # on Python 3.11+, which is our target floor.
    from dataclasses import FrozenInstanceError

    out = ParsedOutput(content="x", extension="py", parse_ok=True)
    with pytest.raises(FrozenInstanceError):
        out.content = "y"  # type: ignore[misc]


def test_parsed_output_parse_error_defaults_none() -> None:
    out = ParsedOutput(content="x", extension="py", parse_ok=True)
    assert out.parse_error is None


# ---------------------------------------------------------------------------
# CodeMode.frame_prompt
# ---------------------------------------------------------------------------


def test_code_mode_frame_prompt_without_deps_forbids_external_modules() -> None:
    framed = CODE_MODE.frame_prompt("write a snake game")
    assert "single-file" in framed
    assert "Do not include any external modules" in framed
    assert "write a snake game" in framed


def test_code_mode_frame_prompt_with_deps_allows_pypi() -> None:
    mode = CodeMode(allow_deps=True)
    framed = mode.frame_prompt("parse a CSV with pandas")
    assert "third-party packages" in framed
    assert "parse a CSV with pandas" in framed
    # The no-deps phrasing must NOT appear.
    assert "Do not include any external modules" not in framed


def test_code_mode_frame_prompt_preserves_registry_default() -> None:
    # Constructing CodeMode(allow_deps=True) must not mutate the default.
    _ = CodeMode(allow_deps=True)
    assert CODE_MODE.allow_deps is False


# ---------------------------------------------------------------------------
# CodeMode.parse_response
# ---------------------------------------------------------------------------


def test_code_mode_parse_fenced_python() -> None:
    raw = "Here:\n```python\nprint('hi')\n```\n"
    out = CODE_MODE.parse_response(raw)
    assert out.parse_ok is True
    assert "print('hi')" in out.content
    assert out.extension == "py"
    assert out.parse_error is None


def test_code_mode_parse_json_payload() -> None:
    raw = '{"code": "x = 1", "language": "python"}'
    out = CODE_MODE.parse_response(raw)
    assert out.parse_ok is True
    assert out.content.startswith("x = 1")
    assert out.extension == "py"


def test_code_mode_parse_html_block() -> None:
    raw = "```html\n<!DOCTYPE html><html></html>\n```\n"
    out = CODE_MODE.parse_response(raw)
    assert out.parse_ok is True
    assert out.extension == "html"


def test_code_mode_parse_empty_returns_failure() -> None:
    out = CODE_MODE.parse_response("")
    assert out.parse_ok is False
    assert out.extension == ""
    assert out.parse_error is not None
    assert "code extraction failed" in out.parse_error


def test_code_mode_parse_whitespace_returns_failure() -> None:
    out = CODE_MODE.parse_response("   \n  ")
    assert out.parse_ok is False


def test_code_mode_parse_salvages_unclosed_fence() -> None:
    # LLM opened a fence but never closed it — stage 3 salvage.
    raw = "Sure:\n```python\ndef f():\n    return 1\n"
    out = CODE_MODE.parse_response(raw)
    assert out.parse_ok is True
    assert "def f()" in out.content
    assert out.extension == "py"


def test_code_mode_strips_leading_dot_from_extension() -> None:
    # extract_code returns ".py"; ParsedOutput.extension must not include
    # the dot (so callers can compose filenames predictably).
    raw = "```python\nx = 1\n```\n"
    out = CODE_MODE.parse_response(raw)
    assert not out.extension.startswith(".")


# ---------------------------------------------------------------------------
# TextMode.frame_prompt
# ---------------------------------------------------------------------------


def test_text_mode_frame_prompt_nudges_markdown() -> None:
    framed = TEXT_MODE.frame_prompt("explain quantum computing")
    assert "Markdown" in framed
    assert "explain quantum computing" in framed


# ---------------------------------------------------------------------------
# TextMode.parse_response
# ---------------------------------------------------------------------------


def test_text_mode_parse_passes_through_markdown_verbatim() -> None:
    md = "# Hello\n\nSome **bold** text.\n"
    out = TEXT_MODE.parse_response(md)
    assert out.parse_ok is True
    assert out.content == md  # byte-for-byte pass-through
    assert out.extension == "md"
    assert out.parse_error is None


def test_text_mode_parse_empty_returns_failure() -> None:
    out = TEXT_MODE.parse_response("")
    assert out.parse_ok is False
    assert out.extension == "md"
    assert out.parse_error == "empty response"


def test_text_mode_parse_whitespace_returns_failure() -> None:
    out = TEXT_MODE.parse_response("   \n\n\t")
    assert out.parse_ok is False


# ---------------------------------------------------------------------------
# TTSMode
# ---------------------------------------------------------------------------


def test_tts_mode_frame_prompt_preserves_text_to_synthesize() -> None:
    framed = TTS_MODE.frame_prompt("  Hello from WaveBench.  ")
    assert framed == "Hello from WaveBench."


def test_tts_mode_parse_audio_bytes() -> None:
    out = TTS_MODE.parse_response(b"ID3audio")
    assert out.parse_ok is True
    assert out.content == b"ID3audio"
    assert out.extension == "mp3"
    assert out.parse_error is None


def test_tts_mode_parse_empty_audio_returns_failure() -> None:
    out = TTS_MODE.parse_response(b"")
    assert out.parse_ok is False
    assert out.extension == "mp3"
    assert out.parse_error == "empty audio response"


def test_tts_mode_can_configure_voice_and_format() -> None:
    mode = TTSMode(voice="nova", response_format="pcm", speed=1.2)
    out = mode.parse_response(b"\x00\x01")
    assert mode.voice == "nova"
    assert mode.speed == 1.2
    assert out.extension == "pcm"


# ---------------------------------------------------------------------------
# Cross-mode: independence of registry default from runtime instances
# ---------------------------------------------------------------------------


def test_construct_deps_variant_without_touching_registry() -> None:
    deps_mode = CodeMode(allow_deps=True)
    assert deps_mode is not CODE_MODE
    assert MODES["code"] is CODE_MODE
    assert MODES["code"].allow_deps is False  # type: ignore[attr-defined]


def test_text_mode_singleton_identity() -> None:
    assert MODES["text"] is TEXT_MODE
    assert isinstance(TEXT_MODE, TextMode)


def test_tts_mode_singleton_identity() -> None:
    assert MODES["tts"] is TTS_MODE
    assert isinstance(TTS_MODE, TTSMode)
