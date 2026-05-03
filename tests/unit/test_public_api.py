"""Import-compatibility test — every name imported from ``wavebench.*`` by
any other module in the package must remain resolvable post-refactor.

The enumeration below reflects what ``__main__.py`` and inter-package
imports actually reach for (snapshot as of Deliverable #2). When the
decomposition lands, packages with ``__init__.py`` re-exports keep these
names working.
"""

from __future__ import annotations


def test_wavebench_core_public_names() -> None:
    # Everything __main__.py and the rest of the package imports from core.
    from wavebench.core import main_async

    assert callable(main_async)


def test_wavebench_parsers_public_names() -> None:
    from wavebench.parsers import get_directory_name, parse_llm_output

    assert callable(get_directory_name)
    assert callable(parse_llm_output)


def test_wavebench_storage_public_names() -> None:
    from wavebench.storage import (
        _history_path,
        load_config,
        load_history,
        load_models,
        record_run,
        save_config,
        save_history,
        save_models,
    )

    for fn in (
        load_config,
        load_history,
        load_models,
        record_run,
        save_config,
        save_history,
        save_models,
        _history_path,
    ):
        assert callable(fn)


def test_wavebench_models_public_names() -> None:
    from wavebench.models import (
        MODEL_MAPPING,
        TTS_MODEL_MAPPING,
        _model_score,
        is_stealth,
        is_tts_model,
        tts_voice_for_model,
    )

    assert isinstance(MODEL_MAPPING, dict)
    assert isinstance(TTS_MODEL_MAPPING, dict)
    assert callable(_model_score)
    assert callable(is_stealth)
    assert callable(is_tts_model)
    assert callable(tts_voice_for_model)


def test_wavebench_api_public_names() -> None:
    from wavebench.api import (
        _map_effort,
        _supported_efforts,
        call_model_async,
        call_model_streaming,
        call_tts_speech,
        fetch_top_models,
        load_api_key,
    )

    for fn in (
        call_model_async,
        call_model_streaming,
        call_tts_speech,
        fetch_top_models,
        load_api_key,
        _map_effort,
        _supported_efforts,
    ):
        assert callable(fn)


def test_wavebench_tui_progress_public_names() -> None:
    from wavebench.tui.progress import ProgressTracker, render_idle_wave

    assert ProgressTracker is not None
    assert callable(render_idle_wave)


def test_wavebench_tui_analytics_public_names() -> None:
    from wavebench.tui.analytics import compute_cost, display_analytics

    assert callable(compute_cost)
    assert callable(display_analytics)


def test_wavebench_tui_input_public_names() -> None:
    from wavebench.tui.input import _read_key, _read_key_or_resize, _read_key_timeout

    assert callable(_read_key)
    assert callable(_read_key_or_resize)
    assert callable(_read_key_timeout)


def test_wavebench_tui_line_editor_public_names() -> None:
    from wavebench.tui.line_editor import _read_line, _TabEscape

    assert callable(_read_line)
    assert isinstance(_TabEscape, type)  # it's an exception class


def test_wavebench_tui_menus_public_names() -> None:
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


def test_wavebench_tui_styles_public_names() -> None:
    from wavebench.tui.styles import (
        THEMES,
        S,
        _banner,
        _box_bot,
        _box_row,
        _box_top,
        _dot,
        _fail,
        _ok,
        _tw,
        _work,
        apply_theme,
    )

    assert isinstance(THEMES, dict) and len(THEMES) >= 1
    for fn in (_banner, _box_bot, _box_row, _box_top, _dot, _fail, _ok, _tw, _work, apply_theme):
        assert callable(fn) or fn is not None
    # S is a namespace-like class with ANSI attrs.
    assert hasattr(S, "RST")
