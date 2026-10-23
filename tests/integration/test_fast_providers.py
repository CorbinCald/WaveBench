"""Saved speed preferences reach real HTTP requests and benchmark history."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from wavebench import api, storage
from wavebench.core.orchestrator import main_async


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("mode", ["text", "image"])
async def test_saved_preference_reaches_benchmark_requests_and_retries(
    tmp_state_dir, monkeypatch, mode, enabled
):
    requests = []
    model_id = "test/image" if mode == "image" else "test/model"

    async def handler(request):
        data = await request.json()
        requests.append(data)
        if len(requests) == 1:
            return web.Response(status=503, headers={"Retry-After": "0"})
        if mode == "image":
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "images": [{"image_url": {"url": "data:image/png;base64,aGVsbG8="}}]
                            }
                        }
                    ],
                    "usage": {"total_tokens": 2},
                }
            )
        chunk = {
            "choices": [{"delta": {"content": "Hello"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 2},
        }
        return web.Response(
            text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", content_type="text/event-stream"
        )

    monkeypatch.setattr(api, "_MODEL_CONTEXTS_ATTEMPTED", True)
    monkeypatch.setitem(api._MODEL_CONTEXT_CACHE, model_id, 32_000)
    storage.save_config(
        {
            "fast_providers": "on" if enabled else "off",
            "reasoning_effort": "off",
            "auto_open": "off",
            "directory_naming": "slug",
        }
    )
    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        await main_async(
            SimpleNamespace(prompt="Hello", mode=mode, text=False),
            "offline-key",
            {"Fixture": model_id},
            config=storage.load_config(),
            pricing_lookup={model_id: {"__output_modalities": [mode]}},
        )

    assert len(requests) == 2
    for request in requests:
        assert request["model"] == model_id
        assert request.get("provider") == ({"sort": "throughput"} if enabled else None)
    run = storage.load_history()["runs"][0]
    assert run["fast_providers"] is enabled
    result = run["models"]["Fixture"]
    assert result["status"] == "success"
    assert Path(run["output_dir"], result["file"]).exists()


@pytest.mark.parametrize("enabled", [False, True])
async def test_non_streaming_chat_keeps_speed_preference_after_reasoning_fallback(
    monkeypatch, enabled
):
    requests = []

    async def handler(request):
        data = await request.json()
        requests.append(data)
        if "reasoning" in data or "reasoning_effort" in data:
            return web.Response(status=400, text="unsupported reasoning")
        return web.json_response({"choices": [{"message": {"content": "Hello"}}]})

    monkeypatch.setattr(api, "_MODEL_CONTEXTS_ATTEMPTED", True)
    monkeypatch.setitem(api._MODEL_CONTEXT_CACHE, "test/model", 32_000)
    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        assert (
            await api.call_model_async(
                client, "offline-key", "test/model", "Hello", fast_providers=enabled
            )
            == "Hello"
        )
    assert len(requests) > 1
    assert all(
        request.get("provider") == ({"sort": "throughput"} if enabled else None)
        for request in requests
    )
