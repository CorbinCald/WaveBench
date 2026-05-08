"""Characterization tests for ``wavebench/tui/interactive.py``.

SCAFFOLDING — written to pin behavior across the split into
``tui/input.py``, ``tui/line_editor.py``, and ``tui/menus/*``.

Scope:
  - Pure helpers that used to live in interactive.py (``_format_price``,
    ``_generate_short_name``, ``_unique_short_name``, ``_fit``,
    ``_filter_model_indices``, ``_is_printable_search_char``).
  - Import contract for the keyboard primitives, line editor, and menu
    entry points — real interactive behavior can't be tested without a TTY,
    but we can guarantee callability and type shape.

Retire after: Deliverable #2 (interactive) ships and is known-good.
"""

from __future__ import annotations

import inspect

# ---------------------------------------------------------------------------
# _format_price
# ---------------------------------------------------------------------------


def test_format_price_formats_prompt_and_completion() -> None:
    from wavebench.tui.menus._shared import _format_price

    # Per-token pricing → "$X/$Y /M" (per million tokens)
    out = _format_price({"prompt": "0.000001", "completion": "0.000002"})
    assert out == "$1.00/$2.00 /M"


def test_format_price_empty_when_both_zero() -> None:
    from wavebench.tui.menus._shared import _format_price

    assert _format_price({"prompt": "0", "completion": "0"}) == ""


def test_format_price_empty_when_missing() -> None:
    from wavebench.tui.menus._shared import _format_price

    assert _format_price({}) == ""


def test_format_price_invalid_strings_returns_empty() -> None:
    from wavebench.tui.menus._shared import _format_price

    assert _format_price({"prompt": "bad", "completion": "also bad"}) == ""


# ---------------------------------------------------------------------------
# _generate_short_name / _unique_short_name
# ---------------------------------------------------------------------------


def test_generate_short_name_camelcase_from_provider_slug() -> None:
    from wavebench.tui.menus._shared import _generate_short_name

    # "anthropic/claude-opus-4.6" → "claudeOpus4.6"
    assert _generate_short_name("anthropic/claude-opus-4.6") == "claudeOpus4.6"


def test_generate_short_name_handles_underscores() -> None:
    from wavebench.tui.menus._shared import _generate_short_name

    assert _generate_short_name("openai/gpt_4o_mini") == "gpt4oMini"


def test_generate_short_name_digit_first_char_preserves_case() -> None:
    from wavebench.tui.menus._shared import _generate_short_name

    # Parts starting with a digit are appended as-is (no uppercasing).
    assert _generate_short_name("google/gemini-3-pro") == "gemini3Pro"


def test_unique_short_name_returns_base_when_unused() -> None:
    from wavebench.tui.menus._shared import _unique_short_name

    assert _unique_short_name("x/y", set()) == "y"


def test_unique_short_name_disambiguates_collision() -> None:
    from wavebench.tui.menus._shared import _unique_short_name

    existing = {"y"}
    assert _unique_short_name("x/y", existing) == "y_2"


def test_unique_short_name_disambiguates_multiple_collisions() -> None:
    from wavebench.tui.menus._shared import _unique_short_name

    existing = {"y", "y_2", "y_3"}
    assert _unique_short_name("x/y", existing) == "y_4"


# ---------------------------------------------------------------------------
# _fit
# ---------------------------------------------------------------------------


def test_fit_short_text_unchanged() -> None:
    from wavebench.tui.menus._shared import _fit

    assert _fit("hello", 10) == "hello"


def test_fit_exact_width_unchanged() -> None:
    from wavebench.tui.menus._shared import _fit

    assert _fit("abcde", 5) == "abcde"


def test_fit_truncates_with_ellipsis() -> None:
    from wavebench.tui.menus._shared import _fit

    # 11 chars → fit to 5: "abcd…"
    assert _fit("hello world", 5) == "hell…"


# ---------------------------------------------------------------------------
# _filter_model_indices
# ---------------------------------------------------------------------------


def test_filter_model_indices_empty_query_returns_all() -> None:
    from wavebench.tui.menus._shared import _filter_model_indices

    items = [{"short": "a", "id": "p/a"}, {"short": "b", "id": "p/b"}]
    assert _filter_model_indices(items, "") == [0, 1]


def test_filter_model_indices_substring_match_case_insensitive() -> None:
    from wavebench.tui.menus._shared import _filter_model_indices

    items = [
        {"short": "claudeOpus", "id": "anthropic/claude-opus-4.6"},
        {"short": "gemini3", "id": "google/gemini-3-pro"},
    ]
    assert _filter_model_indices(items, "CLAUDE") == [0]
    assert _filter_model_indices(items, "google") == [1]


def test_filter_model_indices_no_match_returns_empty() -> None:
    from wavebench.tui.menus._shared import _filter_model_indices

    items = [{"short": "a", "id": "p/a"}]
    assert _filter_model_indices(items, "zz") == []


# ---------------------------------------------------------------------------
# _is_printable_search_char
# ---------------------------------------------------------------------------


def test_is_printable_search_char_accepts_letters_digits() -> None:
    from wavebench.tui.menus._shared import _is_printable_search_char

    assert _is_printable_search_char("a")
    assert _is_printable_search_char("Z")
    assert _is_printable_search_char("3")


def test_is_printable_search_char_rejects_control_chars() -> None:
    from wavebench.tui.menus._shared import _is_printable_search_char

    assert not _is_printable_search_char("\t")
    assert not _is_printable_search_char("\r")
    assert not _is_printable_search_char("\n")


def test_is_printable_search_char_rejects_multi_char() -> None:
    from wavebench.tui.menus._shared import _is_printable_search_char

    # Keywords like "up", "down", "enter" that _read_key returns as names.
    assert not _is_printable_search_char("up")
    assert not _is_printable_search_char("enter")


# ---------------------------------------------------------------------------
# Import contract — new paths
# ---------------------------------------------------------------------------


def test_input_module_exports_key_readers() -> None:
    from wavebench.tui.input import _read_key, _read_key_or_resize, _read_key_timeout

    assert callable(_read_key)
    assert callable(_read_key_or_resize)
    assert callable(_read_key_timeout)


def test_line_editor_module_exports_read_line_and_tabescape() -> None:
    from wavebench.tui.line_editor import _read_line, _TabEscape

    assert callable(_read_line)
    assert inspect.isclass(_TabEscape)
    assert issubclass(_TabEscape, Exception)


def test_config_menu_builds_separate_default_model_tabs() -> None:
    from wavebench.models import IMAGE_MODEL_MAPPING, MODEL_MAPPING, TTS_MODEL_MAPPING
    from wavebench.tui.menus.config_menu import (
        _build_config_model_items,
        _filter_config_model_indices,
    )

    items = _build_config_model_items([], {}, {})
    normal_ids = {items[i]["id"] for i in _filter_config_model_indices(items, "", tts=False)}
    tts_ids = {items[i]["id"] for i in _filter_config_model_indices(items, "", tts=True)}
    image_ids = {items[i]["id"] for i in _filter_config_model_indices(items, "", image=True)}

    assert set(MODEL_MAPPING.values()).issubset(normal_ids)
    assert set(TTS_MODEL_MAPPING.values()).issubset(tts_ids)
    assert set(IMAGE_MODEL_MAPPING.values()).issubset(image_ids)
    assert normal_ids.isdisjoint(tts_ids)
    assert normal_ids.isdisjoint(image_ids)
    assert tts_ids.isdisjoint(image_ids)


def test_config_menu_seeds_tts_defaults_when_current_mapping_has_only_text() -> None:
    from wavebench.models import TTS_MODEL_MAPPING
    from wavebench.tui.menus.config_menu import (
        _build_config_model_items,
        _filter_config_model_indices,
    )

    items = _build_config_model_items(
        [], {"claude": "anthropic/claude-opus-4.6"}, pricing_lookup={}
    )
    selected_tts_ids = {
        items[i]["id"]
        for i in _filter_config_model_indices(items, "", tts=True)
        if items[i]["selected"]
    }

    assert set(TTS_MODEL_MAPPING.values()).issubset(selected_tts_ids)


def test_config_menu_filters_catalog_models_into_matching_tabs() -> None:
    from wavebench.tui.menus.config_menu import (
        _build_config_model_items,
        _filter_config_model_indices,
    )

    items = _build_config_model_items(
        [
            {"id": "anthropic/claude-opus-4.6"},
            {"id": "google/gemini-3.1-flash-tts-preview"},
            {
                "id": "openai/test-image",
                "architecture": {"output_modalities": ["image", "text"]},
            },
        ],
        {},
        pricing_lookup={},
    )

    normal_matches = [items[i]["id"] for i in _filter_config_model_indices(items, "claude", tts=False)]
    tts_matches = [items[i]["id"] for i in _filter_config_model_indices(items, "tts", tts=True)]
    image_matches = [
        items[i]["id"] for i in _filter_config_model_indices(items, "test-image", image=True)
    ]

    assert normal_matches == ["anthropic/claude-opus-4.6"]
    assert "google/gemini-3.1-flash-tts-preview" in tts_matches
    assert image_matches == ["openai/test-image"]


def test_config_menu_includes_catalog_models_beyond_old_100_item_cap() -> None:
    from wavebench.tui.menus.config_menu import (
        _build_config_model_items,
        _filter_config_model_indices,
    )

    available = [
        {
            "id": f"provider/model-{i}",
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        }
        for i in range(150)
    ]

    items = _build_config_model_items(available, {"selected": "provider/selected"}, {})
    model_ids = [items[i]["id"] for i in _filter_config_model_indices(items, "", tts=False)]

    assert "provider/model-149" in model_ids


def test_menus_package_exports_public_entries() -> None:
    from wavebench.tui.menus import (
        interactive_config_menu,
        interactive_model_menu,
        run_config_menu,
        run_model_selection,
    )

    for fn in (
        interactive_config_menu,
        interactive_model_menu,
        run_config_menu,
        run_model_selection,
    ):
        assert callable(fn)
