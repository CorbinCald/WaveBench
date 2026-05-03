"""Unit coverage for TTS-specific orchestration decisions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from wavebench.core import orchestrator as orchestrator_mod
from wavebench.modes.tts import TTSMode


def test_resolve_tts_mode_uses_cli_options_over_config() -> None:
    args = SimpleNamespace(
        mode="tts",
        text=False,
        tts_voice="nova",
        tts_format="pcm",
        tts_speed=1.25,
    )

    mode = orchestrator_mod._resolve_mode(
        args,
        auto_install="off",
        config={"tts_voice": "alloy", "tts_format": "mp3", "tts_speed": 1.0},
    )

    assert isinstance(mode, TTSMode)
    assert mode.voice == "nova"
    assert mode.response_format == "pcm"
    assert mode.speed == 1.25


async def test_main_async_tts_filters_selected_models_to_tts(
    tmp_state_dir: Path,
    monkeypatch,
) -> None:
    async def fake_get_directory_name(*_args: Any, **_kwargs: Any) -> str:
        return "tts_outputs"

    seen: list[tuple[str, str, str, str | None, str]] = []

    async def fake_run_model(
        mode,
        session,
        api_key,
        name,
        mid,
        user_prompt,
        default_ext,
        output_dir_task,
        semaphore,
        results,
        pad,
        tracker,
        reasoning_effort="high",
        auto_open="off",
        auto_install="off",
    ) -> None:
        seen.append((mode.name, name, mid, reasoning_effort, auto_open))
        output_dir = await output_dir_task
        filename = f"{name}{default_ext}"
        Path(output_dir, filename).write_bytes(b"audio")
        results[name] = {
            "status": "success",
            "time_s": 0.01,
            "file": filename,
            "usage": {"audio_bytes": 5, "input_characters": len(user_prompt)},
            "retries": [],
        }

    monkeypatch.setattr(orchestrator_mod, "get_directory_name", fake_get_directory_name)
    monkeypatch.setattr(orchestrator_mod, "run_model", fake_run_model)
    monkeypatch.setattr(orchestrator_mod, "_open_with_viewer", lambda _path: None)

    args = SimpleNamespace(
        prompt="Hello from WaveBench",
        mode="tts",
        text=False,
        auto_open=None,
        auto_install=None,
        tts_voice=None,
        tts_format=None,
        tts_speed=None,
    )
    mapping = {
        "textModel": "anthropic/claude-opus-4.6",
        "voiceModel": "openai/gpt-4o-mini-tts-2025-12-15",
    }

    await orchestrator_mod.main_async(
        args,
        api_key="test-key",
        model_mapping=mapping,
        config={
            "reasoning_effort": "high",
            "analytics_sort": "runs",
            "auto_open": "after_all",
            "auto_install": "off",
            "directory_naming": "slug",
            "tts_voice": "nova",
            "tts_format": "pcm",
            "tts_speed": 1.2,
        },
        pricing_lookup={},
    )

    assert seen == [("tts", "voiceModel", "openai/gpt-4o-mini-tts-2025-12-15", None, "off")]
    assert (tmp_state_dir / "benchmarkResults" / "tts_outputs" / "voiceModel.pcm").read_bytes() == b"audio"
