from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from wavebench import api
from wavebench.harness import session as module
from wavebench.harness.config import Limits
from wavebench.harness.session import HarnessSession
from wavebench.harness.workspace import allocate_run


@pytest.fixture
async def sessions(tmp_path, monkeypatch):
    async def preflight(self):
        # These cases stop before project execution; sandbox behavior has its own suite.
        pass

    monkeypatch.setattr(module.Runtime, "preflight", preflight)
    monkeypatch.setattr(api, "_MODEL_CONTEXTS_ATTEMPTED", True)
    monkeypatch.setitem(api._MODEL_CONTEXT_CACHE, "fixture/model", 1_000_000)
    run = allocate_run(tmp_path, "failure-metrics", "safe fixture")
    created = []

    def create(client=None, **limits):
        session = HarnessSession(
            run,
            len(created) + 1,
            "Fixture",
            "fixture/model",
            "List the empty workspace.",
            client,
            "offline-key",
            Limits(**limits),
            asyncio.Semaphore(1),
            asyncio.Semaphore(1),
            auto_open="off",
            reasoning_effort=None,
        )
        created.append(session)
        return session

    yield create
    for session in created:
        await session.close()


async def test_stream_failure_diagnostics_and_usage_reach_saved_results(sessions, monkeypatch):
    requests = 0

    async def handler(request):
        nonlocal requests
        requests += 1
        payload = {
            "model": "fixture/model",
            "provider": "Fixture",
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            "choices": [{"delta": {"content": "a" * 500}}],
        }
        return web.Response(
            text=f"data: {json.dumps(payload)}\n\n", content_type="text/event-stream"
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = sessions(client, stream_output_min_bytes=100, stream_output_max_bytes=100)
        await session.build()
    result = json.loads((session.metadata / "result.json").read_text())
    assert result["failure"]["category"] == "stream_limit"
    assert result["failure"]["code"] == "stream_output_limit"
    assert result["failure"]["diagnostics"]["bytes"]["content"] == 500
    assert result["harness"]["turns"][0]["stream"]["bytes"]["content"] == 500
    assert result["harness"]["budget"]["used_tokens"] == 13
    assert result["usage"]["total_tokens"] == 13
    assert result["usage"]["cost"] is None
    assert session.dispatcher.tool_usage["calls"] == 0
    assert requests == 1


async def test_unaffordable_next_request_has_remaining_and_input_estimate(sessions):
    session = sessions(total_tokens=1_000_000)
    session.budget_tokens = 934_688
    session.turns = [
        {
            "phase": "building",
            "usage": {
                "prompt_tokens": 856_594,
                "completion_tokens": 78_094,
                "total_tokens": 934_688,
                "prompt_tokens_details": {"cached_tokens": 800_000},
            },
        }
    ]
    session.prompt_estimate = SimpleNamespace(
        bound=lambda value: 80_687, estimate=lambda value: 70_000
    )
    await session.build()
    result = session.result()
    assert result["failure"]["category"] == "token_budget"
    assert result["failure"]["budget"]["remaining_tokens"] == 65_312
    assert result["failure"]["budget"]["next_input_tokens_estimate"] == 80_687
    assert result["harness"]["budget"]["used_tokens"] == 934_688
    assert result["usage"]["completion_tokens"] == 78_094
    assert len(result["harness"]["turns"]) == 1


async def test_complete_text_without_done_reports_missing_submission(sessions, monkeypatch):
    async def handler(request):
        return web.Response(
            text='data: {"choices":[{"delta":{"content":"Done"},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":1,"total_tokens":11}}\n\ndata: [DONE]\n\n',
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = sessions(client)
        await session.build()
    assert session.result()["failure"]["category"] == "model_protocol"
    assert session.result()["failure"]["code"] == "project_abandoned"
    assert session.result()["failure"]["summary"] == "Model ended without submission"
    assert session.result()["usage"]["total_tokens"] == 11


async def test_cancelled_http_stream_saves_diagnostics_without_invented_usage(
    sessions, monkeypatch
):
    received = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')
        await release.wait()
        return response

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = sessions(client)
        original = session.on_usage

        def usage(values, output):
            original(values, output)
            if output:
                received.set()

        session.on_usage = usage
        task = asyncio.create_task(session.build())
        try:
            await asyncio.wait_for(received.wait(), 2)
            task.cancel()
            await task
        finally:
            release.set()
    result = session.result()
    assert result["status"] == "cancelled"
    assert result["failure"]["category"] == "cancelled"
    assert result["failure"]["diagnostics"]["failure_code"] == "stream_cancelled"
    assert result["harness"]["turns"][0]["stream"]["bytes"]["content"] == 7
    assert result["usage"]["total_tokens"] is None
    assert result["harness"]["budget"]["estimated"] is True
