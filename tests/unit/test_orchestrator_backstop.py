"""Unit coverage for the orchestrator's vanishing-model backstop.

``run_model`` records its own failures, so an exception escaping the task
means it died before writing a result.  ``gather(return_exceptions=True)``
used to swallow that exception silently, and the model was simply absent
from the results box and history — neither pass nor fail.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from wavebench.core import orchestrator as orchestrator_mod


async def test_task_that_dies_before_recording_is_backstopped_as_failed(
    tmp_state_dir: Path,
    monkeypatch,
    capsys,
) -> None:
    async def fake_get_directory_name(*_args: Any, **_kwargs: Any) -> str:
        return "tts_outputs"

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
        await output_dir_task
        raise RuntimeError("exploded before recording a result")

    monkeypatch.setattr(orchestrator_mod, "get_directory_name", fake_get_directory_name)
    monkeypatch.setattr(orchestrator_mod, "run_model", fake_run_model)

    args = SimpleNamespace(
        prompt="Say hello",
        mode="tts",
        text=False,
        auto_open=None,
        auto_install=None,
        tts_voice=None,
        tts_format=None,
        tts_speed=None,
    )

    await orchestrator_mod.main_async(
        args,
        api_key="test-key",
        model_mapping={"voiceModel": "openai/gpt-4o-mini-tts-2025-12-15"},
        config={
            "reasoning_effort": "high",
            "analytics_sort": "runs",
            "auto_open": "off",
            "auto_install": "off",
            "directory_naming": "slug",
            "tts_voice": "nova",
            "tts_format": "pcm",
            "tts_speed": 1.0,
        },
        pricing_lookup={},
    )

    history = json.loads((tmp_state_dir / ".benchmark_history.json").read_text(encoding="utf-8"))
    assert history["runs"][0]["models"]["voiceModel"]["status"] == "failed"
    # The fallback results box shows the reason, not just a bare "failed".
    assert "exploded before" in capsys.readouterr().out
