"""Resolve effort from catalog metadata before any completion is sent."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from wavebench import api
from wavebench.core import orchestrator


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    for name in (
        "_MODEL_CONTEXT_CACHE",
        "_MODEL_MAX_COMPLETION_CACHE",
        "_MODEL_TOOL_CACHE",
        "_MODEL_EFFORT_CACHE",
    ):
        monkeypatch.setattr(api, name, {})
    monkeypatch.setattr(api, "_MODEL_CONTEXTS_ATTEMPTED", False)
    monkeypatch.setattr(api, "_MODEL_CONTEXT_LOCK", asyncio.Lock())


def catalog_model(model, reasoning):
    return {
        "id": model,
        "context_length": 32000,
        "supported_parameters": ["reasoning", "tools"],
        "reasoning": reasoning,
    }


def completion_app(catalog):
    events = []

    async def models(request):
        events.append("catalog")
        return web.json_response({"data": catalog})

    async def completion(request):
        body = await request.json()
        events.append(body)
        if body.get("stream"):
            chunk = {"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]}
            return web.Response(
                text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                content_type="text/event-stream",
            )
        return web.json_response({"choices": [{"message": {"content": "Done"}}]})

    app = web.Application()
    app.router.add_get("/models", models)
    app.router.add_post("/chat/completions", completion)
    return app, events


@pytest.mark.parametrize("transport", ["streaming", "nonstreaming", "harness"])
@pytest.mark.parametrize(
    "model,levels,requested,expected",
    [
        ("meta/muse-wonder", ["max", "xhigh", "high", "medium", "low"], "max", "max"),
        ("new-vendor/new-model", ["max", "low"], "xhigh", "max"),
        ("new-vendor/sparse", ["xhigh", "medium", "minimal"], "high", "xhigh"),
        ("anthropic/claude-opus-4.5", ["max", "high"], "max", "max"),
        ("inception/mercury-new", ["max", "high"], "max", "max"),
    ],
)
async def test_advertised_effort_reaches_first_request(
    monkeypatch, transport, model, levels, requested, expected
):
    app, events = completion_app([catalog_model(model, {"supported_efforts": levels})])
    # A known context size must not bypass reasoning discovery.
    api._MODEL_CONTEXT_CACHE[model] = 32000
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")).rstrip("/"))
        for _ in range(2):
            if transport == "harness":
                turn = await api.call_model_conversation(
                    client,
                    "offline",
                    model,
                    [{"role": "user", "content": "Hello"}],
                    [],
                    max_tokens=1000,
                    reasoning_effort=requested,
                )
                assert turn.message["content"] == "Done"
            else:
                call = (
                    api.call_model_streaming if transport == "streaming" else api.call_model_async
                )
                result = await call(client, "offline", model, "Hello", reasoning_effort=requested)
                assert (result[0] if transport == "streaming" else result) == "Done"
    assert events[0] == "catalog"
    assert events.count("catalog") == 1
    assert [event["reasoning"] for event in events[1:]] == [{"effort": expected}] * 2


@pytest.mark.parametrize(
    "reasoning",
    [
        None,
        {},
        [],
        {"supported_efforts": []},
        {"supported_efforts": "max"},
        {"supported_efforts": ["high", {}]},
        {"supported_efforts": ["future-level"]},
    ],
)
async def test_unusable_metadata_keeps_compatibility_fallback(monkeypatch, reasoning):
    model = "openai/gpt-5.6-sol"
    app, events = completion_app([None, catalog_model(model, reasoning)])
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")).rstrip("/"))
        await api.call_model_async(client, "offline", model, "Hello", reasoning_effort="max")
    assert events[1]["reasoning"] == {"effort": "max"}


async def test_refresh_retains_last_good_efforts_on_failure_and_retries(monkeypatch):
    model = "meta/muse-wonder"
    replies = [
        {"data": [catalog_model(model, {"supported_efforts": ["max", "high"]})]},
        None,
        {"data": [catalog_model(model, {"supported_efforts": ["xhigh", "high"]})]},
        {"data": [catalog_model(model, {})]},
    ]

    async def models(request):
        reply = replies.pop(0)
        return web.json_response(reply) if reply else web.Response(status=503)

    app = web.Application()
    app.router.add_get("/models", models)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")).rstrip("/"))
        await api._load_model_context_lengths(client, "offline")
        await api._load_model_context_lengths(client, "offline", refresh=True)
        assert api._reasoning_attempts(model, "max", 1000)[0]["reasoning"]["effort"] == "max"
        await api._load_model_context_lengths(client, "offline")
        assert api._reasoning_attempts(model, "max", 1000)[0]["reasoning"]["effort"] == "xhigh"
        await api._load_model_context_lengths(client, "offline", refresh=True)
        assert api._supported_efforts(model) == ["low", "medium", "high"]
    assert not replies


async def test_parallel_requests_share_reasoning_preflight(monkeypatch):
    model = "meta/muse-wonder"
    app, events = completion_app([catalog_model(model, {"supported_efforts": ["max", "high"]})])
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")).rstrip("/"))
        await asyncio.gather(
            *(
                api.call_model_async(client, "offline", model, "Hello", reasoning_effort="max")
                for _ in range(3)
            )
        )
    assert events.count("catalog") == 1
    assert all(event["reasoning"] == {"effort": "max"} for event in events[1:])


async def test_each_benchmark_refreshes_before_notices_and_requests(monkeypatch, tmp_state_dir):
    model = "meta/muse-wonder"
    catalog = [catalog_model(model, {"supported_efforts": ["high"]})]
    app, events = completion_app(catalog)
    notices = []
    monkeypatch.setattr(
        orchestrator.ProgressTracker, "set_ticker", lambda self, msgs: notices.extend(msgs)
    )
    args = SimpleNamespace(prompt="Hello", mode="text", text=True, auto_open="off")
    config = {"reasoning_effort": "max", "directory_naming": "slug", "auto_open": "off"}
    async with TestServer(app) as server:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")).rstrip("/"))
        for levels in (["high"], ["max", "high"]):
            catalog[0]["reasoning"]["supported_efforts"] = levels
            await orchestrator.main_async(
                args, "offline", model_mapping={"Wonder": model}, config=config
            )
    assert notices == ["muse-wonder: effort max → high"]
    assert events.count("catalog") == 2
    assert [event["reasoning"]["effort"] for event in events if isinstance(event, dict)] == [
        "high",
        "max",
    ]
