"""Unit coverage for TTS-specific runner behavior."""

from __future__ import annotations

import asyncio
from typing import Any

from wavebench.core import runner as runner_mod
from wavebench.modes.tts import TTSMode


class _NoopTracker:
    is_running = False


async def test_run_model_writes_tts_audio_bytes(
    tmp_path,
    monkeypatch,
) -> None:
    async def fake_call_tts_speech(
        session: Any,
        api_key: str,
        model_id: str,
        input_text: str,
        voice: str,
        response_format: str,
        speed: float,
        on_progress: Any,
        on_retry: Any,
    ) -> tuple[bytes, dict]:
        assert model_id == "openai/test-tts"
        assert input_text == "Hello from WaveBench"
        assert voice == "nova"
        assert response_format == "mp3"
        assert speed == 1.1
        if on_progress:
            on_progress(8)
        return b"ID3audio", {"audio_bytes": 8, "input_characters": len(input_text)}

    monkeypatch.setattr(runner_mod, "call_tts_speech", fake_call_tts_speech)

    async def output_dir() -> str:
        return str(tmp_path)

    output_dir_task = asyncio.create_task(output_dir())
    results: dict[str, Any] = {}

    await runner_mod.run_model(
        TTSMode(voice="nova", response_format="mp3", speed=1.1),
        session=object(),  # type: ignore[arg-type]
        api_key="test-key",
        model_name="voiceModel",
        model_id="openai/test-tts",
        user_prompt="Hello from WaveBench",
        default_ext=".mp3",
        output_dir_task=output_dir_task,
        semaphore=asyncio.Semaphore(1),
        results=results,
        pad=12,
        tracker=_NoopTracker(),
        reasoning_effort=None,
    )

    assert (tmp_path / "voiceModel.mp3").read_bytes() == b"ID3audio"
    assert results["voiceModel"]["status"] == "success"
    assert results["voiceModel"]["file"] == "voiceModel.mp3"
    assert results["voiceModel"]["usage"]["audio_bytes"] == 8


async def test_run_model_maps_default_tts_options_for_bundled_non_openai_models(
    tmp_path,
    monkeypatch,
) -> None:
    seen: list[tuple[str, str, str]] = []

    async def fake_call_tts_speech(
        session: Any,
        api_key: str,
        model_id: str,
        input_text: str,
        voice: str,
        response_format: str,
        speed: float,
        on_progress: Any,
        on_retry: Any,
    ) -> tuple[bytes, dict]:
        seen.append((model_id, voice, response_format))
        return b"audio", {"audio_bytes": 5, "input_characters": len(input_text)}

    monkeypatch.setattr(runner_mod, "call_tts_speech", fake_call_tts_speech)

    async def output_dir() -> str:
        return str(tmp_path)

    results: dict[str, Any] = {}
    output_dir_task = asyncio.create_task(output_dir())
    await runner_mod.run_model(
        TTSMode(),
        session=object(),  # type: ignore[arg-type]
        api_key="test-key",
        model_name="geminiTTS",
        model_id="google/gemini-3.1-flash-tts-preview",
        user_prompt="Hello",
        default_ext=".mp3",
        output_dir_task=output_dir_task,
        semaphore=asyncio.Semaphore(1),
        results=results,
        pad=12,
        tracker=_NoopTracker(),
        reasoning_effort=None,
    )

    assert seen == [("google/gemini-3.1-flash-tts-preview", "Kore", "pcm")]
    assert results["geminiTTS"]["file"] == "geminiTTS.pcm"


async def test_run_model_maps_default_tts_options_for_voxtral(
    tmp_path,
    monkeypatch,
) -> None:
    seen: list[tuple[str, str, str]] = []

    async def fake_call_tts_speech(
        session: Any,
        api_key: str,
        model_id: str,
        input_text: str,
        voice: str,
        response_format: str,
        speed: float,
        on_progress: Any,
        on_retry: Any,
    ) -> tuple[bytes, dict]:
        seen.append((model_id, voice, response_format))
        return b"ID3audio", {"audio_bytes": 8, "input_characters": len(input_text)}

    monkeypatch.setattr(runner_mod, "call_tts_speech", fake_call_tts_speech)

    async def output_dir() -> str:
        return str(tmp_path)

    results: dict[str, Any] = {}
    output_dir_task = asyncio.create_task(output_dir())
    await runner_mod.run_model(
        TTSMode(),
        session=object(),  # type: ignore[arg-type]
        api_key="test-key",
        model_name="voxtralMiniTts2603",
        model_id="mistralai/voxtral-mini-tts-2603",
        user_prompt="Hello",
        default_ext=".mp3",
        output_dir_task=output_dir_task,
        semaphore=asyncio.Semaphore(1),
        results=results,
        pad=20,
        tracker=_NoopTracker(),
        reasoning_effort=None,
    )

    assert seen == [("mistralai/voxtral-mini-tts-2603", "en_paul_neutral", "mp3")]
    assert results["voxtralMiniTts2603"]["file"] == "voxtralMiniTts2603.mp3"
