"""Unit coverage for Image-specific orchestration decisions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from wavebench.core import orchestrator as orchestrator_mod
from wavebench.modes.image import ImageMode


def test_resolve_image_mode_uses_cli_options_as_run_only_custom_settings() -> None:
    args = SimpleNamespace(
        mode="image",
        text=False,
        image_aspect_ratio="1:1",
        image_size="2K",
    )

    mode = orchestrator_mod._resolve_mode(
        args,
        auto_install="off",
        config={
            "image_settings": "provider defaults",
            "image_aspect_ratio": "1:1",
            "image_size": "1K",
        },
    )

    assert isinstance(mode, ImageMode)
    assert mode.custom_settings is True
    assert mode.image_config() == {"aspect_ratio": "1:1", "image_size": "2K"}


def test_resolve_image_mode_provider_defaults_do_not_send_config() -> None:
    args = SimpleNamespace(mode="image", text=False, image_aspect_ratio=None, image_size=None)

    mode = orchestrator_mod._resolve_mode(
        args,
        auto_install="off",
        config={
            "image_settings": "provider defaults",
            "image_aspect_ratio": "16:9",
            "image_size": "1K",
        },
    )

    assert isinstance(mode, ImageMode)
    assert mode.aspect_ratio == "1:1"
    assert mode.custom_settings is False
    assert mode.image_config() is None


async def test_main_async_image_filters_models_generates_gallery_and_skips_history(
    tmp_state_dir: Path,
    monkeypatch,
) -> None:
    async def fake_get_directory_name(*_args: Any, **_kwargs: Any) -> str:
        return "image_outputs"

    seen: list[tuple[str, str, str, str | None, str, list[str] | None]] = []

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
        image_modalities=None,
    ) -> None:
        seen.append((mode.name, name, mid, reasoning_effort, auto_open, image_modalities))
        output_dir = await output_dir_task
        filename = f"{name}.png"
        Path(output_dir, filename).write_bytes(b"image")
        results[name] = {
            "status": "success",
            "time_s": 0.01,
            "file": filename,
            "images": [filename],
            "usage": {"image_count": 1, "image_bytes": 5},
            "retries": [],
        }

    opened: list[str] = []
    monkeypatch.setattr(orchestrator_mod, "get_directory_name", fake_get_directory_name)
    monkeypatch.setattr(orchestrator_mod, "run_model", fake_run_model)
    monkeypatch.setattr(orchestrator_mod, "_open_with_viewer", lambda path: opened.append(path))

    args = SimpleNamespace(
        prompt="A neon wave",
        mode="image",
        text=False,
        auto_open="after_all",
        auto_install=None,
        image_aspect_ratio=None,
        image_size=None,
    )
    mapping = {
        "textModel": "anthropic/claude-opus-4.6",
        "imageModel": "openai/gpt-5.4-image-2",
    }

    await orchestrator_mod.main_async(
        args,
        api_key="test-key",
        model_mapping=mapping,
        config={
            "reasoning_effort": "high",
            "analytics_sort": "runs",
            "auto_open": "off",
            "auto_install": "off",
            "directory_naming": "slug",
            "image_settings": "provider defaults",
            "image_aspect_ratio": "1:1",
            "image_size": "1K",
            "image_model_ids": [],
            "theme": "plum",
        },
        pricing_lookup={
            "openai/gpt-5.4-image-2": {"__output_modalities": ["image", "text"]},
        },
    )

    out_dir = tmp_state_dir / "benchmarkResults" / "image_outputs"
    gallery = out_dir / "gallery.html"
    assert seen == [
        (
            "image",
            "imageModel",
            "openai/gpt-5.4-image-2",
            None,
            "after_all",
            ["image", "text"],
        )
    ]
    assert gallery.exists()
    gallery_html = gallery.read_text(encoding="utf-8")
    assert "imageModel.png" in gallery_html
    assert 'data-theme="plum"' in gallery_html
    assert opened == [str(gallery)]
    assert not (tmp_state_dir / ".benchmark_history.json").exists()


async def test_main_async_image_uses_bundled_defaults_when_no_image_models_selected(
    tmp_state_dir: Path,
    monkeypatch,
) -> None:
    async def fake_get_directory_name(*_args: Any, **_kwargs: Any) -> str:
        return "image_defaults"

    seen_names: list[str] = []

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
        image_modalities=None,
    ) -> None:
        seen_names.append(name)
        output_dir = await output_dir_task
        filename = f"{name}.png"
        Path(output_dir, filename).write_bytes(b"image")
        results[name] = {
            "status": "success",
            "time_s": 0.01,
            "file": filename,
            "images": [filename],
            "usage": {"image_count": 1},
            "retries": [],
        }

    monkeypatch.setattr(orchestrator_mod, "get_directory_name", fake_get_directory_name)
    monkeypatch.setattr(orchestrator_mod, "run_model", fake_run_model)

    args = SimpleNamespace(
        prompt="A wave",
        mode="image",
        text=False,
        auto_open=None,
        auto_install=None,
        image_aspect_ratio=None,
        image_size=None,
    )

    await orchestrator_mod.main_async(
        args,
        api_key="test-key",
        model_mapping={"textModel": "anthropic/claude-opus-4.6"},
        config={
            "reasoning_effort": "high",
            "analytics_sort": "runs",
            "auto_open": "off",
            "auto_install": "off",
            "directory_naming": "slug",
            "image_settings": "provider defaults",
            "image_aspect_ratio": "1:1",
            "image_size": "1K",
            "image_model_ids": [],
        },
        pricing_lookup={},
    )

    assert seen_names == ["gpt5.4Image2", "gemini3.1FlashImage", "riverflowV2Pro"]
