"""Unit coverage for truncation detection in the code-mode runner.

A response cut off by the ``max_tokens`` cap arrives with real content and no
error — it parses, saves, and scores exactly like a complete answer.  The only
thing separating the two is ``finish_reason``, so these tests pin that the
runner reads it and records the distinction on the result.
"""

from __future__ import annotations

import asyncio
from typing import Any

from wavebench.core import runner as runner_mod
from wavebench.modes.code import CodeMode


class _NoopTracker:
    is_running = False


def _fake_stream(usage: dict[str, Any]):
    async def fake_call_model_streaming(*_args: Any, **_kwargs: Any) -> tuple[str, dict]:
        return "```python\nprint('hi')\n```", usage

    return fake_call_model_streaming


async def _run(tmp_path, monkeypatch, usage: dict[str, Any]) -> dict[str, Any]:
    monkeypatch.setattr(runner_mod, "call_model_streaming", _fake_stream(usage))

    async def output_dir() -> str:
        return str(tmp_path)

    results: dict[str, Any] = {}
    await runner_mod.run_model(
        CodeMode(),
        session=object(),  # type: ignore[arg-type]
        api_key="test-key",
        model_name="someModel",
        model_id="vendor/some-model",
        user_prompt="Write a game",
        default_ext=".py",
        output_dir_task=asyncio.create_task(output_dir()),
        semaphore=asyncio.Semaphore(1),
        results=results,
        pad=12,
        tracker=_NoopTracker(),
        reasoning_effort=None,
    )
    return results["someModel"]


async def test_length_finish_reason_flags_the_result_as_truncated(tmp_path, monkeypatch) -> None:
    result = await _run(tmp_path, monkeypatch, {"total_tokens": 128_000, "finish_reason": "length"})
    # Still a success — the file exists and is worth keeping — but marked.
    assert result["status"] == "success"
    assert result["truncated"] is True
    assert result["usage"]["finish_reason"] == "length"


async def test_normal_completion_is_not_flagged(tmp_path, monkeypatch) -> None:
    result = await _run(tmp_path, monkeypatch, {"total_tokens": 42, "finish_reason": "stop"})
    assert result["status"] == "success"
    assert result["truncated"] is False


async def test_missing_finish_reason_is_not_flagged(tmp_path, monkeypatch) -> None:
    # Some providers omit finish_reason entirely; absence is not truncation.
    result = await _run(tmp_path, monkeypatch, {"total_tokens": 42})
    assert result["truncated"] is False
