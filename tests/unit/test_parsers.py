"""Unit tests for ``wavebench.parsers``.

These cover the four stages of ``parse_llm_output``:
  1. Structured JSON payload with ``code`` key
  2. Fenced code-block extraction (``` or ~~~)
  3. Salvage from an unclosed fence
  4. Fallback treating the whole response as code

plus language guessing, extension detection, and edge cases.

``parse_llm_output`` is declared ``async`` for API compatibility but its
``session`` and ``api_key`` params are unused — tests pass ``None`` for both.
"""

from __future__ import annotations

import pytest

from wavebench.parsers import (
    _build_parse_result,
    _extract_json_candidates,
    _guess_language_from_code,
    _lang_to_extension,
    _parse_code_blocks,
    _parse_json_payload,
    _salvage_unclosed_fence,
    _strip_trailing_fence,
    parse_llm_output,
)

# ---------------------------------------------------------------------------
# _lang_to_extension
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "language,expected",
    [
        ("python", ".py"),
        ("PYTHON", ".py"),
        ("py", ".py"),
        ("javascript", ".js"),
        ("ts", ".ts"),
        ("tsx", ".tsx"),
        ("html", ".html"),
        ("go", ".go"),
        ("rust", ".rs"),
        ("bash", ".sh"),
        ("  python  ", ".py"),
        ("", ""),
        ("unknown-language", ""),
    ],
)
def test_lang_to_extension(language: str, expected: str) -> None:
    assert _lang_to_extension(language) == expected


# ---------------------------------------------------------------------------
# _guess_language_from_code
# ---------------------------------------------------------------------------


def test_guess_python_from_shebang() -> None:
    code = "#!/usr/bin/env python3\nprint('hi')\n"
    assert _guess_language_from_code(code) == "python"


def test_guess_python_from_def() -> None:
    code = "import os\n\ndef main():\n    return 0\n"
    assert _guess_language_from_code(code) == "python"


def test_guess_bash_from_shebang() -> None:
    code = "#!/bin/bash\necho hello\n"
    assert _guess_language_from_code(code) == "bash"


def test_guess_html_doctype() -> None:
    assert _guess_language_from_code("<!DOCTYPE html><html></html>") == "html"


def test_guess_html_tag() -> None:
    assert _guess_language_from_code("<html><body></body></html>") == "html"


def test_guess_javascript_function() -> None:
    assert _guess_language_from_code("function hello() { console.log('x'); }") == "javascript"


def test_guess_typescript_from_interface() -> None:
    code = "interface User { name: string; age: number; }"
    assert _guess_language_from_code(code) == "typescript"


def test_guess_typescript_from_type_annotation_arrow() -> None:
    # Arrow-function variant — doesn't match the `function \w+\(` pattern,
    # so the TS type-annotation regex gets a chance to fire.
    code = "const greet = (name: string): number => 0;"
    assert _guess_language_from_code(code) == "typescript"


def test_guess_function_with_types_returns_javascript() -> None:
    # Documents a current quirk: the `function \w+\(` pattern matches before
    # the TS type-annotation check, so a typed function declaration reads as
    # JavaScript. Pinning the behavior; if the guesser is ever improved to
    # detect types in function declarations, this test will fail and should
    # be updated.
    code = "function greet(name: string): number { return 0; }"
    assert _guess_language_from_code(code) == "javascript"


def test_guess_go_package() -> None:
    code = 'package main\n\nfunc main() { fmt.Println("hi") }\n'
    assert _guess_language_from_code(code) == "go"


def test_guess_rust_main() -> None:
    assert _guess_language_from_code('fn main() { println!("hi"); }') == "rust"


def test_guess_json_object() -> None:
    assert _guess_language_from_code('{"a": 1, "b": 2}') == "json"


def test_guess_text_fallback() -> None:
    # A plain sentence with no programming markers falls through to "text".
    assert _guess_language_from_code("Hello, this is prose.") == "text"


# ---------------------------------------------------------------------------
# _strip_trailing_fence
# ---------------------------------------------------------------------------


def test_strip_trailing_fence_backticks() -> None:
    assert _strip_trailing_fence("print('hi')\n```") == "print('hi')"


def test_strip_trailing_fence_tildes() -> None:
    assert _strip_trailing_fence("print('hi')\n~~~") == "print('hi')"


def test_strip_trailing_fence_multiple_passes() -> None:
    # Two dangling fence markers get stripped in a loop.
    src = "print('hi')\n```\n```"
    assert _strip_trailing_fence(src) == "print('hi')"


def test_strip_trailing_fence_no_fence_is_identity() -> None:
    assert _strip_trailing_fence("print('hi')") == "print('hi')"


# ---------------------------------------------------------------------------
# _extract_json_candidates
# ---------------------------------------------------------------------------


def test_extract_json_candidates_from_plain_object() -> None:
    candidates = _extract_json_candidates('{"code": "x"}')
    assert candidates[0] == '{"code": "x"}'


def test_extract_json_candidates_from_fenced_json() -> None:
    text = 'here is json:\n```json\n{"code": "x"}\n```\n'
    candidates = _extract_json_candidates(text)
    assert any(c == '{"code": "x"}' for c in candidates)


def test_extract_json_candidates_from_noisy_braces() -> None:
    text = 'intro\n{"code": "x"}\ntrailing'
    candidates = _extract_json_candidates(text)
    assert any(c == '{"code": "x"}' for c in candidates)


# ---------------------------------------------------------------------------
# _parse_json_payload
# ---------------------------------------------------------------------------


def test_parse_json_payload_valid() -> None:
    payload = _parse_json_payload('{"code": "print(1)", "language": "python"}')
    assert payload is not None
    assert payload["code"] == "print(1)"


def test_parse_json_payload_requires_code_string() -> None:
    # Valid JSON but "code" is missing/empty — should return None.
    assert _parse_json_payload('{"language": "python"}') is None
    assert _parse_json_payload('{"code": ""}') is None
    assert _parse_json_payload('{"code": 42}') is None


def test_parse_json_payload_ignores_malformed() -> None:
    assert _parse_json_payload("not json at all") is None


# ---------------------------------------------------------------------------
# _parse_code_blocks
# ---------------------------------------------------------------------------


def test_parse_code_blocks_single_fenced() -> None:
    text = "Intro.\n```python\nprint('hi')\n```\nOutro."
    blocks = _parse_code_blocks(text)
    assert blocks == [("python", "print('hi')")]


def test_parse_code_blocks_multiple_blocks() -> None:
    text = "```py\nx = 1\n```\n```html\n<h1>y</h1>\n```\n"
    blocks = _parse_code_blocks(text)
    assert len(blocks) == 2
    assert blocks[0][0] == "py"
    assert blocks[1][0] == "html"


def test_parse_code_blocks_tildes() -> None:
    text = "~~~python\nprint('hi')\n~~~\n"
    assert _parse_code_blocks(text) == [("python", "print('hi')")]


def test_parse_code_blocks_skips_empty() -> None:
    text = "```\n   \n```\n"
    assert _parse_code_blocks(text) == []


# ---------------------------------------------------------------------------
# _salvage_unclosed_fence
# ---------------------------------------------------------------------------


def test_salvage_unclosed_fence_recovers_code() -> None:
    text = "```python\ndef f():\n    return 42"
    result = _salvage_unclosed_fence(text)
    assert result is not None
    lang, code = result
    assert lang == "python"
    assert "def f()" in code


def test_salvage_returns_none_without_any_fence() -> None:
    assert _salvage_unclosed_fence("just prose, no fences") is None


# ---------------------------------------------------------------------------
# _build_parse_result
# ---------------------------------------------------------------------------


def test_build_parse_result_trailing_newline_appended() -> None:
    result = _build_parse_result("print('hi')", "python")
    assert result["code"].endswith("\n")
    assert result["extension"] == ".py"
    assert result["language"] == "python"


def test_build_parse_result_language_guessed_when_missing() -> None:
    result = _build_parse_result("package main\nfunc main() {}")
    assert result["language"] == "go"
    assert result["extension"] == ".go"


def test_build_parse_result_extension_hint_wins() -> None:
    # Even if language is wrong, an explicit extension hint should be honored.
    result = _build_parse_result("x = 1", language_hint="python", extension_hint="txt")
    assert result["extension"] == ".txt"


def test_build_parse_result_extension_hint_normalizes_dot() -> None:
    r1 = _build_parse_result("x = 1", language_hint="python", extension_hint="rs")
    r2 = _build_parse_result("x = 1", language_hint="python", extension_hint=".rs")
    assert r1["extension"] == ".rs"
    assert r2["extension"] == ".rs"


# ---------------------------------------------------------------------------
# parse_llm_output — end-to-end over each stage
# ---------------------------------------------------------------------------


async def test_parse_llm_output_stage1_json() -> None:
    content = '{"code": "print(1)", "language": "python"}'
    result = await parse_llm_output(None, None, "dummy-model", content)
    assert result is not None
    assert result["code"].startswith("print(1)")
    assert result["extension"] == ".py"
    assert result["language"] == "python"


async def test_parse_llm_output_stage1_json_fenced() -> None:
    content = 'intro\n```json\n{"code": "x = 1", "language": "python"}\n```\n'
    result = await parse_llm_output(None, None, "dummy-model", content)
    assert result is not None
    assert result["code"].startswith("x = 1")
    assert result["language"] == "python"


async def test_parse_llm_output_stage2_fenced() -> None:
    content = "Here's the code:\n```python\ndef f(): return 1\n```\nDone."
    result = await parse_llm_output(None, None, "dummy-model", content)
    assert result is not None
    assert "def f()" in result["code"]
    assert result["extension"] == ".py"


async def test_parse_llm_output_stage2_prefers_longest_nonjson_block() -> None:
    # A short json block + a longer python block — python should win because
    # non-json is preferred, and within that the longest wins.
    content = (
        '```json\n{"meta": 1}\n```\n'
        "```python\n"
        "def long_function():\n"
        "    # lots of comments\n"
        "    x = 1\n"
        "    y = 2\n"
        "    return x + y\n"
        "```\n"
    )
    result = await parse_llm_output(None, None, "dummy-model", content)
    assert result is not None
    assert "long_function" in result["code"]
    assert result["language"] == "python"


async def test_parse_llm_output_stage3_salvage() -> None:
    # LLM opened a fence but never closed it.
    content = "Here:\n```python\ndef f():\n    return 1\n"
    result = await parse_llm_output(None, None, "dummy-model", content)
    assert result is not None
    assert "def f()" in result["code"]
    assert result["extension"] == ".py"


async def test_parse_llm_output_stage4_fallback_prose_to_text() -> None:
    # No JSON, no fences — treated as raw code-like text with guessed language.
    content = "Hello, this is just prose with no code markers."
    result = await parse_llm_output(None, None, "dummy-model", content)
    assert result is not None
    assert result["language"] == "text"
    # An unknown language leaves extension empty.
    assert result["extension"] == ""


async def test_parse_llm_output_stage4_fallback_guesses_python() -> None:
    content = "import os\ndef main():\n    print('hi')\n"
    result = await parse_llm_output(None, None, "dummy-model", content)
    assert result is not None
    assert result["language"] == "python"
    assert result["extension"] == ".py"


async def test_parse_llm_output_empty_returns_none() -> None:
    assert await parse_llm_output(None, None, "m", "") is None
    assert await parse_llm_output(None, None, "m", "   \n  ") is None
    assert await parse_llm_output(None, None, "m", None) is None  # type: ignore[arg-type]
