"""Unit tests for ``wavebench.modes``.

Covers:
  - ``Mode`` protocol registration (both built-ins present).
  - ``CodeMode`` prompt framing (default vs allow_deps) and response
    parsing across the extraction cascade + failure path.
  - ``TextMode`` prompt framing and pass-through response parsing.
  - ``ParsedOutput`` dataclass shape and frozenness.
"""

from __future__ import annotations

import base64

import pytest

from wavebench.modes import CODE_MODE, IMAGE_MODE, MODES, TEXT_MODE, TTS_MODE, ParsedOutput
from wavebench.modes.code import CodeMode
from wavebench.modes.image import ImageMode, extract_image_outputs, write_image_gallery
from wavebench.modes.text import TextMode
from wavebench.modes.tts import TTSMode

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_both_builtins() -> None:
    assert set(MODES.keys()) == {"harness", "code", "text", "tts", "image"}
    assert MODES["harness"] is MODES["code"]


def test_registry_maps_name_to_canonical_instance() -> None:
    assert MODES["code"] is CODE_MODE
    assert MODES["text"] is TEXT_MODE
    assert MODES["tts"] is TTS_MODE
    assert MODES["image"] is IMAGE_MODE


def test_code_mode_defaults_disallow_deps() -> None:
    assert CODE_MODE.allow_deps is False


def test_mode_identity_attrs() -> None:
    assert CODE_MODE.name == "harness"
    assert CODE_MODE.display_name == "Harness"
    assert TEXT_MODE.name == "text"
    assert TEXT_MODE.display_name == "Text"
    assert TTS_MODE.name == "tts"
    assert TTS_MODE.display_name == "TTS"
    assert IMAGE_MODE.name == "image"
    assert IMAGE_MODE.display_name == "Image"


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
    assert "workspace" in framed and "single-file" not in framed
    assert "Dependencies are disabled" in framed
    assert "write a snake game" in framed


def test_code_mode_frame_prompt_with_deps_allows_pypi() -> None:
    mode = CodeMode(allow_deps=True)
    framed = mode.frame_prompt("parse a CSV with pandas")
    assert "PyPI wheels" in framed
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


# ---------------------------------------------------------------------------
# ImageMode
# ---------------------------------------------------------------------------


def test_image_mode_frame_prompt_preserves_text_to_image_prompt() -> None:
    framed = IMAGE_MODE.frame_prompt("  A neon wave at sunset  ")
    assert framed == "A neon wave at sunset"


def test_image_mode_provider_defaults_do_not_send_image_config() -> None:
    assert IMAGE_MODE.aspect_ratio == "1:1"
    assert IMAGE_MODE.image_config() is None


def test_image_mode_custom_settings_send_image_config() -> None:
    mode = ImageMode(aspect_ratio="1:1", image_size="2K", custom_settings=True)
    assert mode.image_config() == {"aspect_ratio": "1:1", "image_size": "2K"}


def test_image_mode_extracts_base64_data_urls_and_extensions() -> None:
    raw = {
        "content": "assistant text is ignored",
        "images": [
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,aGVsbG8="},
            },
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64,d29ybGQ="},
            },
        ],
    }

    images = extract_image_outputs(raw)

    assert [image.data for image in images] == [b"hello", b"world"]
    assert [image.extension for image in images] == ["png", "jpg"]


def test_image_mode_detects_actual_image_mime_over_data_url_label() -> None:
    webp = b"RIFF\x04\x00\x00\x00WEBPVP8 "
    raw = {"image_url": {"url": "data:image/png;base64," + base64.b64encode(webp).decode()}}

    images = extract_image_outputs(raw)

    assert images[0].mime_type == "image/webp"
    assert images[0].extension == "webp"


def test_image_mode_parse_fails_without_valid_data_url() -> None:
    out = IMAGE_MODE.parse_response({"content": "plain assistant text"})  # type: ignore[arg-type]
    assert out.parse_ok is False
    assert out.parse_error == "no valid base64 image data URLs"


def _b64_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def test_image_mode_rejects_truncated_containers() -> None:
    # b64decode happily decodes any 4-char-aligned prefix of a longer
    # payload, so a stream cut mid-image still yields bytes with valid
    # magic; the container trailer is the only cheap completeness signal.
    raw = {
        "images": [
            {"image_url": {"url": _b64_url("image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)}},
            {"image_url": {"url": _b64_url("image/jpeg", b"\xff\xd8\xff\xe0" + b"\x00" * 16)}},
            {"image_url": {"url": _b64_url("image/gif", b"GIF89a" + b"\x00" * 16)}},
            {"image_url": {"url": _b64_url("image/webp", b"RIFF\x40\x00\x00\x00WEBPVP8 ")}},
        ]
    }
    assert extract_image_outputs(raw) == []


def test_image_mode_accepts_complete_containers() -> None:
    webp_body = b"WEBPVP8 " + b"\x00" * 8
    raw = {
        "images": [
            {
                "image_url": {
                    "url": _b64_url("image/png", b"\x89PNG\r\n\x1a\n\x00\x00IEND\xae\x42\x60\x82")
                }
            },
            {"image_url": {"url": _b64_url("image/jpeg", b"\xff\xd8\xff\xe0\x00\x00\xff\xd9")}},
            {"image_url": {"url": _b64_url("image/gif", b"GIF89a\x00\x00;")}},
            {
                "image_url": {
                    "url": _b64_url(
                        "image/webp",
                        b"RIFF" + len(webp_body).to_bytes(4, "little") + webp_body,
                    )
                }
            },
        ]
    }
    images = extract_image_outputs(raw)
    assert [image.mime_type for image in images] == [
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    ]


def test_image_mode_parse_fails_on_truncated_container() -> None:
    url = _b64_url("image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    out = IMAGE_MODE.parse_response({"images": [{"image_url": {"url": url}}]})  # type: ignore[arg-type]
    assert out.parse_ok is False
    assert out.parse_error == "no valid base64 image data URLs"


def test_image_mode_deduplicates_identical_images_across_fields() -> None:
    # Providers often surface the same image under message.images AND echo
    # the data URL in content; the walk must return it once, not twice.
    url = "data:image/png;base64,aGVsbG8="
    raw = {
        "images": [{"image_url": {"url": url}}],
        "content": f"Here you go: {url}",
    }
    images = extract_image_outputs(raw)
    assert len(images) == 1
    assert images[0].data == b"hello"


def test_image_mode_decodes_hard_wrapped_base64() -> None:
    # Some providers hard-wrap base64 with indented continuation lines.
    encoded = base64.b64encode(b"hello world, hello world").decode()
    wrapped = f"{encoded[:8]}\n    {encoded[8:16]}\n    {encoded[16:]}"
    raw = {"content": f"data:image/png;base64,{wrapped}"}
    images = extract_image_outputs(raw)
    assert len(images) == 1
    assert images[0].data == b"hello world, hello world"


def test_image_mode_recovers_data_url_followed_by_prose() -> None:
    # The whitespace-tolerant payload class swallows trailing words; the
    # decoder must fall back to the first run rather than lose the image.
    raw = {"content": "data:image/png;base64,aGVsbG8= Enjoy the wave"}
    images = extract_image_outputs(raw)
    assert len(images) == 1
    assert images[0].data == b"hello"


def test_image_gallery_includes_summary_actions_and_prompt_formatting(tmp_path) -> None:
    path = write_image_gallery(
        str(tmp_path),
        "A neon wave\nwith clouds",
        {
            "modelOne": {
                "status": "success",
                "images": ["modelOne.png", "modelOne_2.webp"],
            },
            "modelTwo": {"status": "failed"},
        },
        theme_name="plum",
    )

    html = (tmp_path / "gallery.html").read_text(encoding="utf-8")

    assert path == str(tmp_path / "gallery.html")
    assert "2 images" in html
    assert "1 successful model" in html
    assert "1 failed model" in html
    assert '<span class="prompt-label">Prompt</span>' in html
    assert "white-space: pre-wrap" in html
    assert 'loading="lazy"' in html
    assert "Open full size" in html
    assert "Download" in html
    assert "modelOne_2.webp" in html
    assert 'data-theme="plum"' in html
    assert "--wb-accent-rgb: 153, 0, 204;" in html
    assert 'id="image-modal"' in html
    assert 'data-gallery-index="1"' in html
    assert 'data-action="prev"' in html
    assert 'data-action="next"' in html
    assert "ArrowLeft" in html
    assert "ArrowRight" in html
