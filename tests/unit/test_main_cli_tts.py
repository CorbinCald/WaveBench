"""CLI-entry coverage for TTS mode selection."""

from __future__ import annotations

import sys
from typing import Any


def _default_config() -> dict[str, Any]:
    return {
        "reasoning_effort": "high",
        "analytics_sort": "runs",
        "theme": "default",
        "auto_open": "off",
        "auto_install": "off",
        "directory_naming": "llm",
        "tts_voice": "alloy",
        "tts_format": "mp3",
        "tts_speed": 1.0,
    }


def test_mode_tts_flag_skips_interactive_mode_selector(monkeypatch) -> None:
    from wavebench import __main__ as main_mod

    seen: dict[str, Any] = {}

    async def fake_main_async(args, api_key, model_mapping=None, config=None, pricing_lookup=None):
        seen["prompt"] = args.prompt
        seen["mode"] = args.mode
        seen["text"] = args.text
        seen["api_key"] = api_key

    def fail_if_mode_selector_reads_key(_timeout: float):
        raise AssertionError("mode selector should be skipped when --mode is provided")

    monkeypatch.setattr(sys, "argv", ["wavebench", "--mode", "tts"])
    monkeypatch.setattr(main_mod, "load_api_key", lambda: "test-key")
    monkeypatch.setattr(main_mod, "fetch_top_models", lambda *_args, **_kwargs: ([], {}))
    monkeypatch.setattr(main_mod, "load_models", lambda: None)
    monkeypatch.setattr(main_mod, "load_config", _default_config)
    monkeypatch.setattr(main_mod, "apply_theme", lambda _theme: None)
    monkeypatch.setattr(main_mod, "_load_query_history", lambda: None)
    monkeypatch.setattr(main_mod, "_save_query_history", lambda _query: None)
    monkeypatch.setattr(main_mod, "_read_key_timeout", fail_if_mode_selector_reads_key)
    monkeypatch.setattr(
        main_mod,
        "_read_line",
        lambda _prompt, history=None, on_idle=None: "Hello from WaveBench",
    )
    monkeypatch.setattr(main_mod, "main_async", fake_main_async)

    main_mod.main()

    assert seen == {
        "prompt": "Hello from WaveBench",
        "mode": "tts",
        "text": False,
        "api_key": "test-key",
    }
