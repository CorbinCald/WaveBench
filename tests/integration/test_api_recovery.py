"""Real HTTP regressions for failed requests, split Unicode, and stalled streams."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from wavebench import api


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    monkeypatch.setattr(api, "_MODEL_CONTEXT_CACHE", {"test/model": 32000})
    monkeypatch.setattr(api, "_MODEL_MAX_COMPLETION_CACHE", {})
    monkeypatch.setattr(api, "_MODEL_TOOL_CACHE", {})
    monkeypatch.setattr(api, "_MODEL_CONTEXTS_ATTEMPTED", False)
    monkeypatch.setattr(api, "_MODEL_CONTEXT_LOCK", asyncio.Lock())


@pytest.mark.parametrize("status", [401, 402, 404, 500])
async def test_hard_error_is_not_reissued_without_reasoning(monkeypatch, status):
    requests = []

    async def handler(request):
        requests.append(await request.json())
        return web.Response(status=status, text="Request rejected")

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")).rstrip("/"))
        with pytest.raises(RuntimeError, match=f"HTTP {status}"):
            await api.call_model_async(session, "test-key", "test/model", "hello")
    assert len(requests) == 1


async def test_catalog_failure_does_not_disable_later_resolution(monkeypatch):
    requests = 0

    async def handler(request):
        nonlocal requests
        requests += 1
        if requests == 1:
            return web.Response(status=503)
        return web.json_response({"data": [{"id": "test/model", "context_length": 64000}]})

    app = web.Application()
    app.router.add_get("/models", handler)
    api._MODEL_CONTEXT_CACHE.clear()
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")).rstrip("/"))
        await api._load_model_context_lengths(session, "test-key")
        assert not api._MODEL_CONTEXTS_ATTEMPTED
        await api._load_model_context_lengths(session, "test-key")
    assert requests == 2
    assert api._MODEL_CONTEXT_CACHE["test/model"] == 64000


async def test_cancelled_catalog_fetch_can_be_retried(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        started.set()
        await release.wait()
        return web.json_response({"data": [{"id": "test/model", "context_length": 64000}]})

    app = web.Application()
    app.router.add_get("/models", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")).rstrip("/"))
        task = asyncio.create_task(api._load_model_context_lengths(session, "test-key"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not api._MODEL_CONTEXTS_ATTEMPTED
        release.set()
        await api._load_model_context_lengths(session, "test-key")
    assert api._MODEL_CONTEXT_CACHE["test/model"] == 64000


async def test_split_utf8_and_null_events_preserve_text_and_usage(monkeypatch):
    expected = "Hello 🌊 世界"

    async def handler(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        events = [
            {"choices": [{"delta": {"content": expected}}], "usage": {"total_tokens": 7}},
            {"choices": None, "usage": None},
            {"choices": [{"delta": None, "finish_reason": "stop"}], "usage": None},
        ]
        for event in events:
            data = ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
            for byte in data:
                await response.write(bytes([byte]))
                await asyncio.sleep(0.001)
        await response.write(b"data: [DONE]\n\n")
        return response

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")).rstrip("/"))
        text, usage = await api.call_model_streaming(session, "test-key", "test/model", "hello")
    assert text == expected
    assert usage == {"total_tokens": 7, "finish_reason": "stop"}


async def test_stall_after_first_token_is_bounded_without_reissuing(monkeypatch):
    release = asyncio.Event()
    requests = 0
    progress = []

    async def handler(request):
        nonlocal requests
        requests += 1
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
        await release.wait()
        return response

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    monkeypatch.setattr(api, "REASONING_STALL_TIMEOUT", 0.1)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")).rstrip("/"))
        try:
            with pytest.raises(RuntimeError, match="stall"):
                await asyncio.wait_for(
                    api.call_model_streaming(
                        session, "test-key", "test/model", "hello", on_progress=progress.append
                    ),
                    timeout=2,
                )
        finally:
            release.set()
    assert progress == [5]
    assert requests == 1


@pytest.mark.parametrize("status", [429, 502, 503])
async def test_nonstreaming_retries_back_off_and_preserve_request(monkeypatch, status):
    requests, retries = [], []

    async def handler(request):
        requests.append(await request.json())
        if len(requests) < 3:
            return web.Response(status=status, headers={"Retry-After": "0.5"})
        return web.json_response({"choices": [{"message": {"content": "recovered"}}]})

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")).rstrip("/"))
        result = await api.call_model_async(
            session,
            "test-key",
            "test/model",
            "hello",
            on_retry=lambda *event: retries.append(event),
        )
    assert result == "recovered"
    assert requests[0] == requests[1] == requests[2]
    assert [event[3] for event in retries] == [0.5, 0.5]


async def test_exhausted_nonstreaming_retries_do_not_start_reasoning_fallback(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(await request.json())
        return web.Response(status=503)

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    monkeypatch.setattr(api, "_MAX_RETRIES", 1)
    monkeypatch.setattr(api, "_MAX_RETRY_WAIT_S", 0.01)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")).rstrip("/"))
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await api.call_model_async(session, "test-key", "test/model", "hello")
    assert len(requests) == 2
    assert requests[0] == requests[1]
