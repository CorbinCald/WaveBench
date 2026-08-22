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


async def _sse_body(chunks: list[dict], include_done: bool = True) -> bytes:
    """Serialize a list of SSE event dicts into the on-wire byte stream."""
    out = bytearray()
    for chunk in chunks:
        out.extend(b"data: ")
        out.extend(json.dumps(chunk).encode("utf-8"))
        out.extend(b"\n\n")
    if include_done:
        out.extend(b"data: [DONE]\n\n")
    return bytes(out)


def _make_streaming_app(
    chunks: list[dict], status: int = 200, err_body: str = "", include_done: bool = True
) -> web.Application:
    """Build a test app that streams *chunks* on POST to /chat/completions."""

    async def handler(request: web.Request) -> web.StreamResponse:
        if status != 200:
            return web.Response(status=status, text=err_body)
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        body = await _sse_body(chunks, include_done)
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
    api_mod._MODEL_MAX_COMPLETION_CACHE.clear()
    api_mod._MODEL_CONTEXTS_ATTEMPTED = False
    yield
    api_mod._MODEL_CONTEXT_CACHE.clear()
    api_mod._MODEL_MAX_COMPLETION_CACHE.clear()
    api_mod._MODEL_CONTEXTS_ATTEMPTED = False


def test_reasoning_stall_timeout_is_one_hour() -> None:
    assert api_mod.REASONING_STALL_TIMEOUT == 60 * 60


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


async def test_streaming_mid_stream_error_event_raises(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OpenRouter can accept the request (HTTP 200), stream part of an
    # answer, then deliver a terminal error event with no choices at all.
    # Returning the partial content as if complete would score a broken
    # generation as a pass.
    chunks = [
        {"choices": [{"delta": {"content": "partial answer"}}]},
        {"error": {"code": 502, "message": "Provider returned error"}},
    ]
    app = _make_streaming_app(chunks, include_done=False)

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            with pytest.raises(RuntimeError, match="mid-stream error"):
                await api_mod.call_model_streaming(
                    session,
                    api_key="test-key",
                    model_id="test/model",
                    prompt="hi",
                    reasoning_effort=None,
                )


async def test_streaming_eof_without_done_is_marked_incomplete(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # EOF with neither a finish_reason nor the [DONE] sentinel: the
    # connection dropped mid-generation and the tail may be missing.
    chunks = [{"choices": [{"delta": {"content": "partial"}}]}]
    app = _make_streaming_app(chunks, include_done=False)

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

    assert content == "partial"
    assert usage["finish_reason"] == "incomplete"


async def test_streaming_finish_reason_wins_over_missing_done(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A provider that reports finish_reason but forgets the [DONE] sentinel
    # still delivered a complete answer — don't flag it.
    chunks = [{"choices": [{"delta": {"content": "whole answer"}, "finish_reason": "stop"}]}]
    app = _make_streaming_app(chunks, include_done=False)

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            _, usage = await api_mod.call_model_streaming(
                session,
                api_key="test-key",
                model_id="test/model",
                prompt="hi",
                reasoning_effort=None,
            )

    assert usage["finish_reason"] == "stop"


async def test_streaming_done_without_finish_reason_is_not_flagged(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The [DONE] sentinel alone marks a healthy end of stream.
    chunks = [{"choices": [{"delta": {"content": "whole answer"}}]}]
    app = _make_streaming_app(chunks)

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            _, usage = await api_mod.call_model_streaming(
                session,
                api_key="test-key",
                model_id="test/model",
                prompt="hi",
                reasoning_effort=None,
            )

    assert "finish_reason" not in usage


# ---------------------------------------------------------------------------
# Image generation endpoint
# ---------------------------------------------------------------------------


async def test_image_generation_posts_non_streaming_chat_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: web.Request) -> web.Response:
        seen["body"] = await request.json()
        return web.json_response(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "images": [{"image_url": {"url": "data:image/png;base64,aGVsbG8="}}],
                        }
                    }
                ],
                "usage": {"total_tokens": 5},
            }
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            message, usage = await api_mod.call_image_generation(
                session,
                api_key="test-key",
                model_id="openai/test-image",
                prompt="A wave",
                modalities=["image", "text"],
                image_config={"aspect_ratio": "16:9", "image_size": "1K"},
            )

    assert seen["body"] == {
        "model": "openai/test-image",
        "messages": [{"role": "user", "content": "A wave"}],
        "modalities": ["image", "text"],
        "stream": False,
        "image_config": {"aspect_ratio": "16:9", "image_size": "1K"},
    }
    assert message["images"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert usage == {"total_tokens": 5}


# ---------------------------------------------------------------------------
# TTS audio endpoint
# ---------------------------------------------------------------------------


async def test_tts_speech_posts_audio_request_and_returns_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: web.Request) -> web.Response:
        seen["body"] = await request.json()
        return web.Response(
            body=b"ID3audio",
            headers={"Content-Type": "audio/mpeg", "X-Generation-Id": "gen_123"},
        )

    app = web.Application()
    app.router.add_post("/audio/speech", handler)

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            audio, usage = await api_mod.call_tts_speech(
                session,
                api_key="test-key",
                model_id="openai/test-tts",
                input_text="Hello",
                voice="nova",
                response_format="mp3",
                speed=1.25,
            )

    assert audio == b"ID3audio"
    assert seen["body"] == {
        "model": "openai/test-tts",
        "input": "Hello",
        "voice": "nova",
        "response_format": "mp3",
        "speed": 1.25,
    }
    assert usage == {"input_characters": 5, "audio_bytes": 8, "generation_id": "gen_123"}


async def test_tts_speech_retries_429_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_mod, "_retry_wait_seconds", lambda h, a: 0.0)

    call_log: list[int] = []

    async def handler(request: web.Request) -> web.Response:
        if len(call_log) < 2:
            call_log.append(429)
            return web.Response(status=429, text="upstream throttled")
        call_log.append(200)
        return web.Response(body=b"ID3audio", headers={"Content-Type": "audio/mpeg"})

    retries: list[tuple[int, int, int, float]] = []
    app = web.Application()
    app.router.add_post("/audio/speech", handler)

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            audio, usage = await api_mod.call_tts_speech(
                session,
                api_key="test-key",
                model_id="openai/test-tts",
                input_text="Hello",
                on_retry=lambda *args: retries.append(args),
            )

    assert audio == b"ID3audio"
    assert usage["audio_bytes"] == 8
    assert call_log == [429, 429, 200]
    assert [r[0] for r in retries] == [429, 429]
    assert [r[1] for r in retries] == [1, 2]


async def test_tts_speech_reports_byte_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={"Content-Type": "audio/mpeg"})
        await resp.prepare(request)
        await resp.write(b"abc")
        await resp.write(b"defg")
        await resp.write_eof()
        return resp

    progress_updates: list[int] = []
    app = web.Application()
    app.router.add_post("/audio/speech", handler)

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            audio, _ = await api_mod.call_tts_speech(
                session,
                api_key="test-key",
                model_id="openai/test-tts",
                input_text="Hello",
                on_progress=progress_updates.append,
            )

    assert audio == b"abcdefg"
    assert progress_updates[-1] == len(audio)
    assert all(a <= b for a, b in pairwise(progress_updates))


async def test_tts_speech_http_error_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=400, text='{"error":"bad voice"}')

    app = web.Application()
    app.router.add_post("/audio/speech", handler)

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            with pytest.raises(RuntimeError, match="HTTP 400"):
                await api_mod.call_tts_speech(
                    session,
                    api_key="test-key",
                    model_id="openai/test-tts",
                    input_text="Hello",
                )


# ---------------------------------------------------------------------------
# Model catalog fetch
# ---------------------------------------------------------------------------


def test_fetch_top_models_includes_reserved_speech_and_image_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_urls: list[str] = []

    def model(mid: str, output_modalities: list[str]) -> dict:
        return {
            "id": mid,
            "canonical_slug": mid,
            "name": mid,
            "created": 0,
            "architecture": {
                "input_modalities": ["text"],
                "output_modalities": output_modalities,
            },
            "pricing": {"prompt": "0.000001", "completion": "0.000001"},
            "supported_parameters": [],
            "context_length": 8192,
        }

    body = {
        "data": [
            model("provider/text-a", ["text"]),
            model("provider/text-b", ["text"]),
            model("provider/text-c", ["text"]),
            model("openai/gpt-4o-mini-tts-2025-12-15", ["speech"]),
            model("mistralai/voxtral-mini-tts-2603", ["speech"]),
            model("provider/image", ["image"]),
            model("provider/audio", ["audio"]),
        ]
    }

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(body).encode()

    def fake_urlopen(req: object, timeout: int) -> FakeResponse:
        requested_urls.append(req.full_url)  # type: ignore[attr-defined]
        assert timeout == 15
        return FakeResponse()

    monkeypatch.setattr(api_mod.urllib.request, "urlopen", fake_urlopen)

    models, pricing = api_mod.fetch_top_models("test-key", count=4)

    ids = [m["id"] for m in models]
    assert requested_urls == [f"{api_mod.API_URL}/models?output_modalities=all"]
    assert len(ids) == 4
    assert "openai/gpt-4o-mini-tts-2025-12-15" in ids
    assert "mistralai/voxtral-mini-tts-2603" in ids
    assert "provider/image" in ids
    assert "provider/audio" not in ids
    assert pricing["provider/image"]["__output_modalities"] == ["image"]


def test_fetch_top_models_keeps_free_model_when_no_non_free_counterpart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def model(mid: str) -> dict:
        return {
            "id": mid,
            "canonical_slug": mid.removesuffix(":free"),
            "name": mid,
            "created": 0,
            "architecture": {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": [],
            "context_length": 8192,
        }

    body = {
        "data": [
            model("provider/has-paid:free"),
            model("provider/has-paid"),
            model("provider/free-only:free"),
        ]
    }

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(body).encode()

    def fake_urlopen(req: object, timeout: int) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setattr(api_mod.urllib.request, "urlopen", fake_urlopen)

    models, _pricing = api_mod.fetch_top_models("test-key", count=10)

    ids = [m["id"] for m in models]
    assert "provider/has-paid" in ids
    assert "provider/has-paid:free" not in ids
    assert "provider/free-only:free" in ids


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


def test_supported_efforts_qwen_38_27b_uses_advertised_sparse_ladder() -> None:
    assert api_mod._supported_efforts("qwen/qwen3.8-27b") == ["low", "medium", "xhigh"]


def test_qwen_38_27b_max_reaches_wire_as_xhigh() -> None:
    model_id = "qwen/qwen3.8-27b"
    levels = api_mod._supported_efforts(model_id)
    mapped = api_mod._map_effort("max", levels)

    assert mapped == "xhigh"
    assert api_mod._reasoning_attempts(model_id, "max", 128_000)[0] == {
        "reasoning": {"effort": "xhigh"}
    }
    # This is an explicit model adjustment, so the UI must not suppress it.
    assert api_mod._is_effort_naming_bridge(model_id, "max", mapped) is False


def test_supported_efforts_opus_47_returns_five_levels() -> None:
    levels = api_mod._supported_efforts("anthropic/claude-opus-4.7")
    assert levels == ["low", "medium", "high", "xhigh", "max"]


def test_supported_efforts_legacy_claude_returns_none() -> None:
    # Legacy Claude variants (without a known capability pattern) report
    # None so callers fall back to reasoning.enabled: true.
    assert api_mod._supported_efforts("anthropic/claude-sonnet-3.5") is None


def test_supported_efforts_deepseek_v4_pro_includes_xhigh() -> None:
    assert api_mod._supported_efforts("deepseek/deepseek-v4-pro") == [
        "low",
        "medium",
        "high",
        "xhigh",
    ]


def test_supported_efforts_deepseek_v4_flash_includes_xhigh() -> None:
    assert api_mod._supported_efforts("deepseek/deepseek-v4-flash") == [
        "low",
        "medium",
        "high",
        "xhigh",
    ]


def test_map_effort_preserves_xhigh_on_deepseek_v4() -> None:
    # Regression: before the V4 capability entry, xhigh clamped to high.
    levels = api_mod._supported_efforts("deepseek/deepseek-v4-pro")
    assert api_mod._map_effort("xhigh", levels) == "xhigh"


def test_map_effort_clamps_max_to_xhigh_on_deepseek_v4() -> None:
    # V4's OpenRouter enum doesn't include `max`; closest legal value is xhigh.
    levels = api_mod._supported_efforts("deepseek/deepseek-v4-pro")
    assert api_mod._map_effort("max", levels) == "xhigh"


def test_effort_naming_bridge_skips_v4_max_to_xhigh() -> None:
    # V4 natively calls its max tier `max`; OpenRouter calls the same tier
    # `xhigh`. The mapping is a naming bridge, not a downgrade — UIs should
    # skip the "effort adjusted" notice in this case.
    assert api_mod._is_effort_naming_bridge("deepseek/deepseek-v4-pro", "max", "xhigh") is True
    assert api_mod._is_effort_naming_bridge("deepseek/deepseek-v4-flash", "max", "xhigh") is True


def test_supported_efforts_gpt_55_includes_xhigh() -> None:
    assert api_mod._supported_efforts("openai/gpt-5.5") == [
        "low",
        "medium",
        "high",
        "xhigh",
    ]


def test_supported_efforts_gpt_55_pro_includes_xhigh() -> None:
    assert api_mod._supported_efforts("openai/gpt-5.5-pro") == [
        "low",
        "medium",
        "high",
        "xhigh",
    ]


def test_map_effort_clamps_max_to_xhigh_on_gpt_55() -> None:
    # OpenRouter's GPT-5.5 enum accepts xhigh|high|medium|low|minimal|none
    # and rejects literal `max`; closest legal value is xhigh.
    levels = api_mod._supported_efforts("openai/gpt-5.5")
    assert api_mod._map_effort("max", levels) == "xhigh"


def test_effort_naming_bridge_skips_gpt_55_max_to_xhigh() -> None:
    # GPT-5.5's top reasoning tier is named `xhigh` by OpenRouter's
    # normalization layer; user-configured `max` reaches the same tier, so
    # the "effort adjusted" notice should be suppressed.
    assert api_mod._is_effort_naming_bridge("openai/gpt-5.5", "max", "xhigh") is True
    assert api_mod._is_effort_naming_bridge("openai/gpt-5.5-pro", "max", "xhigh") is True


def test_supported_efforts_gpt_5_family_includes_xhigh() -> None:
    # Whole GPT-5 family shares the same OpenRouter enum
    # (xhigh|high|medium|low|minimal|none) — verified 2026-04-25.
    for slug in [
        "openai/gpt-5",
        "openai/gpt-5-pro",
        "openai/gpt-5-mini",
        "openai/gpt-5-nano",
        "openai/gpt-5-codex",
    ]:
        assert api_mod._supported_efforts(slug) == [
            "low",
            "medium",
            "high",
            "xhigh",
        ], slug


def test_effort_naming_bridge_skips_gpt_5_pro_max_to_xhigh() -> None:
    assert api_mod._is_effort_naming_bridge("openai/gpt-5", "max", "xhigh") is True
    assert api_mod._is_effort_naming_bridge("openai/gpt-5-pro", "max", "xhigh") is True


def test_effort_naming_bridge_allows_real_downgrades_through() -> None:
    # max → high on a non-reasoning model IS a real downgrade and should
    # still surface to the user.
    assert api_mod._is_effort_naming_bridge("google/gemini-3-pro", "max", "high") is False
    # xhigh → max on a Claude 4.6 model is a real clamp (4.6 lacks xhigh);
    # not a naming bridge.
    assert api_mod._is_effort_naming_bridge("anthropic/claude-opus-4.6", "xhigh", "max") is False
    # A V4 downgrade that isn't max→xhigh (e.g. xhigh→high on some
    # hypothetical future V4 variant that dropped xhigh) stays visible.
    assert api_mod._is_effort_naming_bridge("deepseek/deepseek-v4-pro", "xhigh", "high") is False


# ---------------------------------------------------------------------------
# Claude 5 family effort support
#
# Regression guard: the capability table once listed only 4.x patterns, so
# `anthropic/claude-opus-5` matched nothing, fell through to `return None`
# ("legacy Claude"), and the user's xhigh/max was dropped from the wire
# entirely in favour of `{"reasoning": {"enabled": True}}`.
# ---------------------------------------------------------------------------


def test_supported_efforts_claude_5_family_has_full_ladder() -> None:
    # Verified 2026-07-25 against OpenRouter: every tier is accepted and
    # distinct on claude-opus-5 (551 → 1,280 → 1,645 → 1,852 reasoning
    # tokens for low → high → xhigh → max on one identical prompt).
    for slug in [
        "anthropic/claude-opus-5",
        "anthropic/claude-sonnet-5",
        "anthropic/claude-fable-5",
        "anthropic/claude-mythos-5",
        "anthropic/claude-opus-4.8",
    ]:
        assert api_mod._supported_efforts(slug) == [
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ], slug


def test_claude_5_effort_reaches_the_wire() -> None:
    # The bug was invisible at the _supported_efforts level for anyone not
    # reading the None branch, so assert on the actual payload.
    for effort in ("xhigh", "max"):
        primary = api_mod._reasoning_attempts("anthropic/claude-opus-5", effort, 16000)[0]
        assert primary == {"reasoning": {"effort": effort}}, effort


def test_unknown_modern_claude_slug_keeps_effort() -> None:
    # An unrecognised Claude id must not silently lose the effort setting:
    # over-guessing falls forward on a 400, under-guessing fails silently.
    assert api_mod._supported_efforts("anthropic/claude-opus-6") == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


def test_pre_effort_claude_models_still_return_none() -> None:
    # Both slug orderings ("claude-3.7-sonnet" and "claude-sonnet-3.5").
    for slug in [
        "anthropic/claude-sonnet-4.5",
        "anthropic/claude-haiku-4.5",
        "anthropic/claude-sonnet-4",
        "anthropic/claude-3.7-sonnet",
        "anthropic/claude-sonnet-3.5",
        "anthropic/claude-3-haiku",
        "anthropic/claude-2",
    ]:
        assert api_mod._supported_efforts(slug) is None, slug


# ---------------------------------------------------------------------------
# GPT version parsing / the 5.6+ `max` tier
#
# Regression guard: the substring test `"gpt-5" in model_id` swept every
# newer release into the pre-5.6 family's `xhigh` ceiling, so `max` on
# gpt-5.6-sol was clamped to `xhigh` *and* the downgrade notice was
# suppressed by _is_effort_naming_bridge.
# ---------------------------------------------------------------------------


def test_gpt_version_parsing() -> None:
    assert api_mod._gpt_version("openai/gpt-5.6-sol") == (5, 6)
    assert api_mod._gpt_version("openai/gpt-5.5-pro") == (5, 5)
    assert api_mod._gpt_version("openai/gpt-5-pro") == (5, 0)
    assert api_mod._gpt_version("openai/gpt-4o") == (4, 0)
    # Date suffixes must not be read as a minor version.
    assert api_mod._gpt_version("openai/gpt-5-2025-12-15") == (5, 0)
    assert api_mod._gpt_version("anthropic/claude-opus-5") is None


def test_supported_efforts_gpt_56_includes_max() -> None:
    # Verified 2026-07-25 on openai/gpt-5.6-sol via OpenRouter: one identical
    # prompt spent 7,434 reasoning tokens at `high`, 10,105 at `xhigh` and
    # 13,984 at `max` — `max` is a real tier, not an alias for `xhigh`.
    assert api_mod._supported_efforts("openai/gpt-5.6-sol") == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


def test_gpt_56_max_is_not_clamped() -> None:
    levels = api_mod._supported_efforts("openai/gpt-5.6-sol")
    assert api_mod._map_effort("max", levels) == "max"
    primary = api_mod._reasoning_attempts("openai/gpt-5.6-sol", "max", 16000)[0]
    assert primary == {"reasoning": {"effort": "max"}}


def test_gpt_56_max_to_xhigh_is_not_a_naming_bridge() -> None:
    # Should 5.6+ ever be clamped, the loss is real and must stay visible.
    assert api_mod._is_effort_naming_bridge("openai/gpt-5.6-sol", "max", "xhigh") is False


def test_date_suffixed_gpt_5_keeps_pre_56_ceiling() -> None:
    # "gpt-5-2025-12-15" is plain GPT-5, not version 5.2025.
    assert api_mod._supported_efforts("openai/gpt-5-2025-12-15") == [
        "low",
        "medium",
        "high",
        "xhigh",
    ]


# ---------------------------------------------------------------------------
# Output-token budgeting and truncation detection
#
# A benchmark run of an isometric RTS came back as two "successes" that both
# stopped mid-statement: the 32k ceiling cut them off, and because nothing
# looked at ``finish_reason`` the partial files were saved and scored as
# complete.  These pin both halves of the fix — a ceiling high enough for a
# real single-file program, and a signal when a model still hits it.
# ---------------------------------------------------------------------------


def test_output_ceiling_matches_frontier_model_limits() -> None:
    # Claude Opus 5 and GPT-5.6 Sol both advertise max_completion_tokens=128000.
    assert api_mod.MAX_OUTPUT_TOKENS_DEFAULT == 128_000
    # The step-down used when a model rejects that ceiling.
    assert api_mod.MAX_OUTPUT_TOKENS_FALLBACK == 32_000


def test_reasoning_budget_does_not_scale_past_provider_cap() -> None:
    # The reasoning.max_tokens form targets Gemini/Qwen-style thinking
    # budgets, which top out near 32k — 80% of a 128k ceiling would be
    # rejected outright.
    def _budgets(attempts: list[dict]) -> list[int]:
        return [
            a["reasoning"]["max_tokens"] for a in attempts if "max_tokens" in a.get("reasoning", {})
        ]

    budgets = _budgets(api_mod._reasoning_attempts("google/gemini-3-pro", "high", 128_000))
    assert budgets, "expected a reasoning.max_tokens attempt"
    assert max(budgets) <= 32_768
    # Unchanged for the old ceiling: 0.8 * 32_000 was already under the cap.
    assert _budgets(api_mod._reasoning_attempts("google/gemini-3-pro", "high", 32_000)) == [25_600]


async def test_resolve_max_tokens_clamps_to_provider_output_limit(
    _reset_ctx_cache: None,
) -> None:
    # A 1M context window is not a 1M output budget.
    api_mod._MODEL_CONTEXT_CACHE["big/ctx-small-out"] = 1_000_000
    api_mod._MODEL_MAX_COMPLETION_CACHE["big/ctx-small-out"] = 8_192
    async with aiohttp.ClientSession() as session:
        resolved = await api_mod._resolve_max_tokens(
            session, "test-key", "big/ctx-small-out", "hi", fallback=200_000
        )
    assert resolved == 8_192


async def test_resolve_max_tokens_uses_full_ceiling_when_provider_allows(
    _reset_ctx_cache: None,
) -> None:
    api_mod._MODEL_CONTEXT_CACHE["big/roomy"] = 1_000_000
    api_mod._MODEL_MAX_COMPLETION_CACHE["big/roomy"] = 128_000
    async with aiohttp.ClientSession() as session:
        resolved = await api_mod._resolve_max_tokens(
            session, "test-key", "big/roomy", "hi", fallback=200_000
        )
    assert resolved == 128_000


async def test_model_metadata_populates_output_limit_cache(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def models(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "data": [
                    {
                        "id": "vendor/capped",
                        "context_length": 1_000_000,
                        "top_provider": {"max_completion_tokens": 64_000},
                    },
                    # Roughly an eighth of the catalog advertises nothing here.
                    {"id": "vendor/unknown", "context_length": 200_000},
                    {
                        "id": "vendor/null-provider",
                        "context_length": 200_000,
                        "top_provider": None,
                    },
                ]
            }
        )

    app = web.Application()
    app.router.add_get("/models", models)

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            await api_mod._load_model_context_lengths(session, "test-key")

    assert api_mod._MODEL_MAX_COMPLETION_CACHE["vendor/capped"] == 64_000
    assert "vendor/unknown" not in api_mod._MODEL_MAX_COMPLETION_CACHE
    assert "vendor/null-provider" not in api_mod._MODEL_MAX_COMPLETION_CACHE
    # Context lengths still land regardless of the output-limit field.
    assert api_mod._MODEL_CONTEXT_CACHE["vendor/unknown"] == 200_000


def test_usage_with_finish_carries_finish_reason() -> None:
    body = {
        "usage": {"total_tokens": 10},
        "choices": [{"finish_reason": "length", "message": {"content": "partial"}}],
    }
    usage = api_mod._usage_with_finish(body)
    assert usage["finish_reason"] == "length"
    assert usage["total_tokens"] == 10


def test_usage_with_finish_omits_missing_reason() -> None:
    assert "finish_reason" not in api_mod._usage_with_finish({"usage": {"total_tokens": 1}})
    assert api_mod._usage_with_finish({}) == {}
    # A null finish_reason mid-stream must not be recorded as a real one.
    assert "finish_reason" not in api_mod._usage_with_finish({"choices": [{"finish_reason": None}]})


def test_usage_with_finish_does_not_mutate_response_body() -> None:
    body = {"usage": {"total_tokens": 5}, "choices": [{"finish_reason": "stop"}]}
    api_mod._usage_with_finish(body)
    assert "finish_reason" not in body["usage"]


async def test_streaming_surfaces_length_finish_reason(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This is what a truncated benchmark result looks like on the wire: real
    # content, no error, and finish_reason "length" on the final chunk.
    chunks = [
        {"choices": [{"delta": {"content": "def main("}, "finish_reason": None}]},
        {
            "choices": [{"delta": {"content": "self"}, "finish_reason": "length"}],
            "usage": {"total_tokens": 128_000},
        },
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

    assert content == "def main(self"
    assert usage["finish_reason"] == "length"


async def test_streaming_reports_stop_for_complete_responses(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [{"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}]
    app = _make_streaming_app(chunks)

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            _, usage = await api_mod.call_model_streaming(
                session,
                api_key="test-key",
                model_id="test/model",
                prompt="hi",
                reasoning_effort=None,
            )

    assert usage["finish_reason"] == "stop"


async def test_streaming_steps_down_when_provider_rejects_the_ceiling(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A model that advertises no max_completion_tokens gets the optimistic
    # 128k request.  If it 400s in a wording we can't parse, one step down to
    # the old ceiling has to rescue the result rather than failing the model.
    seen: list[int] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        seen.append(body["max_tokens"])
        if body["max_tokens"] > api_mod.MAX_OUTPUT_TOKENS_FALLBACK:
            return web.Response(status=400, text="max_tokens is too large for this model")
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(await _sse_body([{"choices": [{"delta": {"content": "ok"}}]}]))
        await resp.write_eof()
        return resp

    async def models(request: web.Request) -> web.Response:
        # No top_provider block — the 13% of the catalog we can't pre-clamp.
        return web.json_response({"data": [{"id": "test/model", "context_length": 1_000_000}]})

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    app.router.add_get("/models", models)

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

    assert content == "ok"
    assert seen[0] == api_mod.MAX_OUTPUT_TOKENS_DEFAULT
    assert seen[-1] == api_mod.MAX_OUTPUT_TOKENS_FALLBACK


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


# ---------------------------------------------------------------------------
# Retry-on-throttle (429/5xx)
# ---------------------------------------------------------------------------


def test_retry_wait_honors_retry_after_header() -> None:
    # A numeric Retry-After (in seconds) wins over exponential backoff.
    assert api_mod._retry_wait_seconds("3", attempt=1) == 3.0
    assert api_mod._retry_wait_seconds("0", attempt=2) == 0.5  # floored


def test_retry_wait_falls_back_to_exponential_backoff() -> None:
    # No Retry-After → 1, 2, 4 seconds for attempts 1..3.
    assert api_mod._retry_wait_seconds(None, attempt=1) == 1.0
    assert api_mod._retry_wait_seconds(None, attempt=2) == 2.0
    assert api_mod._retry_wait_seconds(None, attempt=3) == 4.0


def test_retry_wait_caps_at_max_to_bound_test_runtime() -> None:
    # A pathological Retry-After must not park the benchmark indefinitely.
    assert api_mod._retry_wait_seconds("3600", attempt=1) == api_mod._MAX_RETRY_WAIT_S
    # Exponential backoff is also capped.
    assert api_mod._retry_wait_seconds(None, attempt=20) == api_mod._MAX_RETRY_WAIT_S


def test_retryable_status_set_includes_429_and_5xx_throttles() -> None:
    assert 429 in api_mod._RETRYABLE_STATUSES
    assert 502 in api_mod._RETRYABLE_STATUSES
    assert 503 in api_mod._RETRYABLE_STATUSES
    assert 504 in api_mod._RETRYABLE_STATUSES
    # 500 and 400 are NOT retried — they signal real errors.
    assert 500 not in api_mod._RETRYABLE_STATUSES
    assert 400 not in api_mod._RETRYABLE_STATUSES


def _make_throttling_then_streaming_app(
    throttle_count: int,
    throttle_status: int,
    chunks: list[dict],
) -> tuple[web.Application, list[int]]:
    """Server that returns *throttle_status* the first *throttle_count* times,
    then streams *chunks*. The returned list records each request's status so
    the test can assert call ordering.
    """
    call_log: list[int] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        if len(call_log) < throttle_count:
            call_log.append(throttle_status)
            return web.Response(status=throttle_status, text="upstream throttled")
        call_log.append(200)
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(await _sse_body(chunks))
        await resp.write_eof()
        return resp

    async def models(request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "test/model", "context_length": 32000}]})

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    app.router.add_get("/models", models)
    return app, call_log


async def test_streaming_retries_429_then_succeeds(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Patch the wait so the test runs in milliseconds, not seconds.
    monkeypatch.setattr(api_mod, "_retry_wait_seconds", lambda h, a: 0.0)

    chunks = [{"choices": [{"delta": {"content": "ok"}}]}]
    app, call_log = _make_throttling_then_streaming_app(
        throttle_count=2, throttle_status=429, chunks=chunks
    )

    retries: list[tuple[int, int, int, float]] = []

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            content, _ = await api_mod.call_model_streaming(
                session,
                api_key="test-key",
                model_id="test/model",
                prompt="hi",
                reasoning_effort=None,
                on_retry=lambda *args: retries.append(args),
            )

    assert content == "ok"
    assert call_log == [429, 429, 200]
    # on_retry fires once per backoff sleep — same count as 429s.
    assert len(retries) == 2
    # First arg of each event is the status that triggered the retry.
    assert all(r[0] == 429 for r in retries)
    # Attempts are 1-based and monotonic.
    assert [r[1] for r in retries] == [1, 2]


async def test_streaming_exhausts_retries_then_raises(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_mod, "_retry_wait_seconds", lambda h, a: 0.0)
    # Force a hard ceiling: throttle on every call.
    chunks: list[dict] = []
    app, call_log = _make_throttling_then_streaming_app(
        throttle_count=999, throttle_status=503, chunks=chunks
    )
    retries: list[tuple[int, int, int, float]] = []

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            with pytest.raises(RuntimeError, match="HTTP 503"):
                await api_mod.call_model_streaming(
                    session,
                    api_key="test-key",
                    model_id="test/model",
                    prompt="hi",
                    reasoning_effort=None,
                    on_retry=lambda *args: retries.append(args),
                )

    # Total POSTs = 1 initial + _MAX_RETRIES retries.
    assert len(call_log) == api_mod._MAX_RETRIES + 1
    # on_retry fires once per backoff sleep — exactly _MAX_RETRIES times.
    assert len(retries) == api_mod._MAX_RETRIES


async def test_streaming_retry_sends_identical_payload_every_attempt(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every retry must re-send the exact same headers and JSON body as the
    initial attempt. Transient throttles are supposed to recover *without*
    changing the measurement — if a future change silently lowered
    max_tokens (or stripped reasoning) across retries, the benchmark would
    be comparing apples to oranges. Pin the invariant with byte-equality.
    """
    monkeypatch.setattr(api_mod, "_retry_wait_seconds", lambda h, a: 0.0)

    bodies: list[bytes] = []
    header_snapshots: list[dict[str, str]] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        bodies.append(await request.read())
        header_snapshots.append({k: v for k, v in request.headers.items() if k.lower() != "host"})
        if len(bodies) <= 2:
            return web.Response(status=429, text="upstream throttled")
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(await _sse_body([{"choices": [{"delta": {"content": "ok"}}]}]))
        await resp.write_eof()
        return resp

    async def models(request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "test/model", "context_length": 32000}]})

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    app.router.add_get("/models", models)

    async with _running_server(app) as server:
        monkeypatch.setattr(api_mod, "API_URL", str(server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            content, _ = await api_mod.call_model_streaming(
                session,
                api_key="test-key",
                model_id="test/model",
                prompt="hi",
                reasoning_effort="high",
            )

    assert content == "ok"
    assert len(bodies) == 3, "expected exactly 3 POSTs (2 throttles + 1 success)"
    # Byte-equality across all retries — no silent parameter drift.
    assert bodies[0] == bodies[1] == bodies[2]
    # Headers carry the same auth/content-type on every attempt. Drop
    # transport-variable fields (connection state) before comparing.
    _drop = {"content-length", "accept-encoding", "user-agent"}
    hdrs = [{k: v for k, v in h.items() if k.lower() not in _drop} for h in header_snapshots]
    assert hdrs[0] == hdrs[1] == hdrs[2]


async def test_streaming_does_not_retry_non_throttle_errors(
    _reset_ctx_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 500 is intentionally NOT in _RETRYABLE_STATUSES — it should fail fast.
    monkeypatch.setattr(api_mod, "_retry_wait_seconds", lambda h, a: 0.0)
    app, call_log = _make_throttling_then_streaming_app(
        throttle_count=999, throttle_status=500, chunks=[]
    )
    retries: list[tuple[int, int, int, float]] = []

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
                    on_retry=lambda *args: retries.append(args),
                )

    # Exactly one POST, zero retries.
    assert call_log == [500]
    assert retries == []
