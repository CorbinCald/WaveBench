"""Unit coverage for truncation detection and failure recording in the runner.

A response cut off by the ``max_tokens`` cap arrives with real content and no
error — it parses, saves, and scores exactly like a complete answer.  The only
thing separating the two is ``finish_reason``, so these tests pin that the
runner reads it and records the distinction on the result.

Failure paths live here too: a mid-stream API error, a refusal that yields no
code, or an exception after the API call must all land in ``results`` as
``failed`` with a reason — never escape the task and leave the model absent
from the results box.
"""

from __future__ import annotations

import asyncio
from typing import Any

from wavebench.core import runner as runner_mod
from wavebench.modes.text import TextMode


class _NoopTracker:
    is_running = False


_CODE_REPLY = "```python\nprint('hi')\n```"


async def _run(
    tmp_path,
    monkeypatch,
    usage: dict[str, Any],
    *,
    content: str = _CODE_REPLY,
    stream_exc: Exception | None = None,
    output_dir_exc: Exception | None = None,
) -> dict[str, Any]:
    async def fake_call_model_streaming(*_args: Any, **_kwargs: Any) -> tuple[str, dict]:
        if stream_exc is not None:
            raise stream_exc
        return content, usage

    monkeypatch.setattr(runner_mod, "call_model_streaming", fake_call_model_streaming)

    async def output_dir() -> str:
        if output_dir_exc is not None:
            raise output_dir_exc
        return str(tmp_path)

    results: dict[str, Any] = {}
    await runner_mod.run_model(
        TextMode(),
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


async def test_incomplete_finish_reason_flags_the_result_as_truncated(
    tmp_path, monkeypatch
) -> None:
    # The api layer stamps "incomplete" when the SSE stream hit EOF without
    # the [DONE] sentinel: the connection dropped and the tail may be gone.
    result = await _run(tmp_path, monkeypatch, {"total_tokens": 10, "finish_reason": "incomplete"})
    assert result["status"] == "success"
    assert result["truncated"] is True


async def test_normal_completion_is_not_flagged(tmp_path, monkeypatch) -> None:
    result = await _run(tmp_path, monkeypatch, {"total_tokens": 42, "finish_reason": "stop"})
    assert result["status"] == "success"
    assert result["truncated"] is False


async def test_missing_finish_reason_is_not_flagged(tmp_path, monkeypatch) -> None:
    # Some providers omit finish_reason entirely; absence is not truncation.
    result = await _run(tmp_path, monkeypatch, {"total_tokens": 42})
    assert result["truncated"] is False


async def test_mid_stream_error_records_failure_with_reason(tmp_path, monkeypatch) -> None:
    # OpenRouter can accept the request (HTTP 200), stream half an answer,
    # then send a terminal error event; api.py surfaces it as RuntimeError.
    result = await _run(
        tmp_path,
        monkeypatch,
        {},
        stream_exc=RuntimeError("mid-stream error (502): provider unavailable"),
    )
    assert result["status"] == "failed"
    assert "mid-stream error" in result["error"]
    assert result["file"] is None


async def test_blank_text_records_parse_failure_with_reason(tmp_path, monkeypatch) -> None:
    result = await _run(tmp_path, monkeypatch, {"total_tokens": 12}, content="   \n  ")
    assert result["status"] == "failed"
    assert "empty response" in result["error"]
    assert result["file"] is None


async def test_post_api_failure_is_recorded_not_lost(tmp_path, monkeypatch) -> None:
    # An exception after the API call (here: the shared output-dir task
    # failing) used to escape the task, get swallowed by
    # gather(return_exceptions=True), and leave the model absent from the
    # results box — neither pass nor fail.
    result = await _run(
        tmp_path, monkeypatch, {"total_tokens": 5}, output_dir_exc=OSError("disk full")
    )
    assert result["status"] == "failed"
    assert "disk full" in result["error"]
