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
