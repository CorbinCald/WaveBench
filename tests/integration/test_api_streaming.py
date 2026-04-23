"""Integration tests for ``wavebench.api`` streaming.

These spin up a local ``aiohttp`` test server that serves canned SSE events,
then call ``call_model_streaming`` against it. The goal is to exercise the
real SSE parser, progress-callback plumbing, and retry logic without hitting
OpenRouter.

We monkeypatch ``wavebench.api.API_URL`` to point at the test server so the
module code is exercised verbatim.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from itertools import pairwise

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from wavebench import api as api_mod


async def _sse_body(chunks: list[dict]) -> bytes:
    """Serialize a list of SSE event dicts into the on-wire byte stream."""
    out = bytearray()
    for chunk in chunks:
        out.extend(b"data: ")
        out.extend(json.dumps(chunk).encode("utf-8"))
        out.extend(b"\n\n")
    out.extend(b"data: [DONE]\n\n")
    return bytes(out)


def _make_streaming_app(
    chunks: list[dict], status: int = 200, err_body: str = ""
) -> web.Application:
    """Build a test app that streams *chunks* on POST to /chat/completions."""

    async def handler(request: web.Request) -> web.StreamResponse:
        if status != 200:
            return web.Response(status=status, text=err_body)
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        body = await _sse_body(chunks)
        # Write in a few pieces to make sure the parser handles multi-chunk input.
        mid = len(body) // 2
        await resp.write(body[:mid])
        await resp.write(body[mid:])
        await resp.write_eof()
        return resp

    async def models(request: web.Request) -> web.Response:
        # api._resolve_max_tokens fetches /models on first use; serve a stub.
        return web.json_response(
            {
                "data": [{"id": "test/model", "context_length": 32000}],
            }
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    app.router.add_get("/models", models)
    return app


@pytest.fixture
def _reset_ctx_cache() -> None:
    """Clear the module-level model-context cache so tests don't share state."""
    api_mod._MODEL_CONTEXT_CACHE.clear()
    api_mod._MODEL_CONTEXTS_ATTEMPTED = False
    yield
    api_mod._MODEL_CONTEXT_CACHE.clear()
    api_mod._MODEL_CONTEXTS_ATTEMPTED = False


@asynccontextmanager
async def _running_server(app: web.Application):
    """Run *app* on a random port for the duration of the block."""
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


async def test_streaming_accumulates_content_across_chunks(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [
        {"choices": [{"delta": {"content": "Hello "}}]},
        {"choices": [{"delta": {"content": "world"}}]},
        {"choices": [{"delta": {"content": "!"}}], "usage": {"total_tokens": 3}},
    ]
    app = _make_streaming_app(chunks)

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            content, usage = await api_mod.call_model_streaming(
                session,
                api_key="test-key",
                model_id="test/model",
                prompt="hi",
                reasoning_effort=None,
            )

    assert content == "Hello world!"
    assert usage.get("total_tokens") == 3


async def test_streaming_reports_progress_as_bytes_flow(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [
        {"choices": [{"delta": {"content": "abc"}}]},
        {"choices": [{"delta": {"content": "defg"}}]},
    ]
    app = _make_streaming_app(chunks)
    progress_updates: list[int] = []

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            content, _ = await api_mod.call_model_streaming(
                session,
                api_key="test-key",
                model_id="test/model",
                prompt="hi",
                reasoning_effort=None,
                on_progress=progress_updates.append,
            )

    assert content == "abcdefg"
    # Progress reports are monotonic non-decreasing character counts.
    assert progress_updates[-1] == len(content)
    assert all(a <= b for a, b in pairwise(progress_updates))


async def test_streaming_handles_null_delta_content(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OpenRouter occasionally sends ``content: null`` while streaming
    # reasoning tokens. The api module safely coerces nulls to empty strings.
    chunks = [
        {"choices": [{"delta": {"content": None, "reasoning": "thinking..."}}]},
        {"choices": [{"delta": {"content": "answer"}}]},
    ]
    app = _make_streaming_app(chunks)

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            content, _ = await api_mod.call_model_streaming(
                session,
                api_key="test-key",
                model_id="test/model",
                prompt="hi",
                reasoning_effort=None,
            )

    # Only the actual content gets accumulated; reasoning tokens don't leak.
    assert content == "answer"


async def test_streaming_http_error_raises_runtime_error(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_streaming_app(chunks=[], status=500, err_body="internal server error")

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            with pytest.raises(RuntimeError, match="HTTP 500"):
                await api_mod.call_model_streaming(
                    session,
                    api_key="test-key",
                    model_id="test/model",
                    prompt="hi",
                    reasoning_effort=None,
                )


# ---------------------------------------------------------------------------
# Pure helpers in api.py that don't need a server
# ---------------------------------------------------------------------------


def test_context_limit_parser_handles_400_text() -> None:
    text = "error: maximum context length is 16384 tokens, but got 20000"
    assert api_mod._context_limit_from_error_text(text) == 16384


def test_context_limit_parser_none_when_missing() -> None:
    assert api_mod._context_limit_from_error_text("some other error") is None


def test_credit_limit_parser_extracts_affordable_tokens() -> None:
    text = (
        "You requested up to 128000 tokens, but can only afford "
        "24,576 tokens on your credit balance."
    )
    assert api_mod._credit_token_limit_from_error(text) == 24576


def test_credit_limit_parser_none_when_missing() -> None:
    assert api_mod._credit_token_limit_from_error("some other 402 reason") is None


# ---------------------------------------------------------------------------
# Reasoning-effort clamping
# ---------------------------------------------------------------------------


def test_map_effort_passes_through_when_supported() -> None:
    assert api_mod._map_effort("high", ["low", "medium", "high"]) == "high"


def test_map_effort_clamps_down_when_unsupported() -> None:
    # xhigh is not in the supported list; should clamp to the closest available.
    # ties resolve upward (highest ordinal wins).
    assert api_mod._map_effort("xhigh", ["low", "medium", "high"]) == "high"


def test_map_effort_clamps_max_to_high() -> None:
    assert api_mod._map_effort("max", ["low", "medium", "high"]) == "high"


def test_supported_efforts_non_anthropic_returns_low_medium_high() -> None:
    assert api_mod._supported_efforts("google/gemini-3-pro") == ["low", "medium", "high"]


def test_supported_efforts_opus_47_returns_five_levels() -> None:
    levels = api_mod._supported_efforts("anthropic/claude-opus-4.7")
    assert levels == ["low", "medium", "high", "xhigh", "max"]


def test_supported_efforts_legacy_claude_returns_none() -> None:
    # Legacy Claude variants (without a known capability pattern) report
    # None so callers fall back to reasoning.enabled: true.
    assert api_mod._supported_efforts("anthropic/claude-sonnet-3.5") is None


# ---------------------------------------------------------------------------
# load_api_key
# ---------------------------------------------------------------------------


def test_load_api_key_from_env(
    tmp_state_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    assert api_mod.load_api_key() == "sk-from-env"


def test_load_api_key_from_dotenv_file(
    tmp_state_dir,
    isolated_env: pytest.MonkeyPatch,
) -> None:
    (tmp_state_dir / ".env").write_text('OPENROUTER_API_KEY="sk-from-file"\n')
    assert api_mod.load_api_key() == "sk-from-file"


def test_load_api_key_none_when_absent(
    tmp_state_dir,
    isolated_env: pytest.MonkeyPatch,
) -> None:
    # No env var, no .env file — should return None cleanly.
    assert api_mod.load_api_key() is None


def test_load_api_key_env_takes_precedence_over_file(
    tmp_state_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    (tmp_state_dir / ".env").write_text("OPENROUTER_API_KEY=sk-from-file\n")
    assert api_mod.load_api_key() == "sk-from-env"
