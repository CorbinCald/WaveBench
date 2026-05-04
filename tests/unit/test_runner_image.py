"""Unit coverage for Image-specific runner behavior."""

from __future__ import annotations

import asyncio
from typing import Any

from wavebench.core import runner as runner_mod
from wavebench.modes.image import ImageMode


class _NoopTracker:
    is_running = False


async def test_run_model_writes_multiple_generated_images_with_detected_extensions(
    tmp_path,
    monkeypatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_call_image_generation(
        session: Any,
        api_key: str,
        model_id: str,
        prompt: str,
        modalities: list[str] | None,
        image_config: dict[str, str] | None,
        on_retry: Any,
    ) -> tuple[dict[str, Any], dict]:
        seen.update(
            {
                "model_id": model_id,
                "prompt": prompt,
                "modalities": modalities,
                "image_config": image_config,
            }
        )
        return (
            {
                "role": "assistant",
                "content": "text response ignored",
                "images": [
                    {"image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    {"image_url": {"url": "data:image/webp;base64,d29ybGQ="}},
                ],
            },
            {"total_tokens": 3},
        )

    monkeypatch.setattr(runner_mod, "call_image_generation", fake_call_image_generation)

    async def output_dir() -> str:
        return str(tmp_path)

    results: dict[str, Any] = {}
    output_dir_task = asyncio.create_task(output_dir())

    await runner_mod.run_model(
        ImageMode(aspect_ratio="1:1", image_size="2K", custom_settings=True),
        session=object(),  # type: ignore[arg-type]
        api_key="test-key",
        model_name="imageModel",
        model_id="openai/test-image",
        user_prompt="A wave",
        default_ext=".png",
        output_dir_task=output_dir_task,
        semaphore=asyncio.Semaphore(1),
        results=results,
        pad=12,
        tracker=_NoopTracker(),
        reasoning_effort=None,
        image_modalities=["image", "text"],
    )

    assert seen == {
        "model_id": "openai/test-image",
        "prompt": "A wave",
        "modalities": ["image", "text"],
        "image_config": {"aspect_ratio": "1:1", "image_size": "2K"},
    }
    assert (tmp_path / "imageModel.png").read_bytes() == b"hello"
    assert (tmp_path / "imageModel_2.webp").read_bytes() == b"world"
    assert results["imageModel"]["status"] == "success"
    assert results["imageModel"]["file"] == "imageModel.png"
    assert results["imageModel"]["images"] == ["imageModel.png", "imageModel_2.webp"]
    assert results["imageModel"]["usage"]["image_count"] == 2


async def test_run_model_fails_image_without_valid_data_url(tmp_path, monkeypatch) -> None:
    async def fake_call_image_generation(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict]:
        return {"role": "assistant", "content": "plain text only"}, {}

    monkeypatch.setattr(runner_mod, "call_image_generation", fake_call_image_generation)

    async def output_dir() -> str:
        return str(tmp_path)

    results: dict[str, Any] = {}
    output_dir_task = asyncio.create_task(output_dir())

    await runner_mod.run_model(
        ImageMode(),
        session=object(),  # type: ignore[arg-type]
        api_key="test-key",
        model_name="imageModel",
        model_id="openai/test-image",
        user_prompt="A wave",
        default_ext=".png",
        output_dir_task=output_dir_task,
        semaphore=asyncio.Semaphore(1),
        results=results,
        pad=12,
        tracker=_NoopTracker(),
        reasoning_effort=None,
    )

    assert results["imageModel"]["status"] == "failed"
    assert not list(tmp_path.iterdir())
