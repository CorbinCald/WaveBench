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
from wavebench.harness.failure import failure_summary
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


@pytest.mark.parametrize("phase_expires_first", [False, True])
async def test_header_wait_failure_is_saved_without_inventing_usage_or_retrying(
    sessions, monkeypatch, phase_expires_first
):
    release = asyncio.Event()
    requests = 0

    async def handler(request):
        nonlocal requests
        requests += 1
        await release.wait()
        return web.Response()

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = sessions(
            client,
            response_headers_seconds=3 if phase_expires_first else 1,
            build_seconds=1 if phase_expires_first else 3,
        )
        try:
            await asyncio.wait_for(session.build(), 3)
        finally:
            release.set()
    result = json.loads((session.metadata / "result.json").read_text())
    failure = result["failure"]
    assert failure["code"] == (
        "time_or_turn_limit" if phase_expires_first else "response_headers_timeout"
    )
    if not phase_expires_first:
        assert failure["category"] == "request_timeout"
        assert failure_summary(result) == "No response headers received"
        assert failure["diagnostics"]["policy"] == {"response_headers_seconds": 1}
    turn = result["harness"]["turns"][0]
    assert turn["stream"]["stage"] == "response_headers"
    assert turn["stream"]["failure_code"] == (
        "request_cancelled" if phase_expires_first else "response_headers_timeout"
    )
    assert turn["usage"] == {}
    assert result["usage"]["api_turns"] == 1
    assert result["usage"]["total_tokens"] is None and result["usage"]["cost"] is None
    assert result["harness"]["budget"]["estimated"]
    assert result["harness"]["budget"]["used_tokens"] > 0
    assert result["harness"]["recoveries"] == [] and result["retries"] == []
    assert session.dispatcher.tool_usage["calls"] == 0 and requests == 1


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


@pytest.mark.parametrize("build_turns,expected_requests", [(32, 2), (1, 1)])
async def test_complete_text_without_done_reports_missing_submission(
    sessions, monkeypatch, build_turns, expected_requests
):
    requests = []

    async def handler(request):
        requests.append(await request.json())
        return web.Response(
            text='data: {"choices":[{"delta":{"content":"Done"},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":1,"total_tokens":11}}\n\ndata: [DONE]\n\n',
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = sessions(client, build_turns=build_turns)
        await session.build()
    assert session.result()["failure"]["category"] == "model_protocol"
    assert session.result()["failure"]["code"] == "project_abandoned"
    assert session.result()["failure"]["summary"] == "Model ended without submission"
    assert session.result()["usage"]["total_tokens"] == 11 * expected_requests
    assert len(requests) == expected_requests
    assert session.descriptor is None and session.attempts == []
    if expected_requests == 2:
        assert "has not received a submission" in requests[1]["messages"][-1]["content"]


def tool_response(command, call_id):
    return {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "wb", "arguments": json.dumps(command)},
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
    }


@pytest.mark.parametrize("recovery", ["submission_reminder", "empty_provider_retry"])
async def test_recovery_preserves_project_and_accounts_for_every_request(
    sessions, monkeypatch, recovery
):
    requests = []
    write = tool_response(
        {"command": "write", "path": "main.py", "content": "print(42)\n"}, "write"
    )
    interrupted = (
        {
            "choices": [{"delta": {"content": "Implemented; ready."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
        }
        if recovery == "submission_reminder"
        else {
            "error": {"code": 503, "message": "private provider body"},
            "usage": {"prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100},
        }
    )
    responses = [
        write,
        interrupted,
        tool_response({"command": "done", "runtime": "python", "entry": "main.py"}, "done"),
    ]

    async def handler(request):
        requests.append(await request.json())
        payload = responses[len(requests) - 1]
        return web.Response(
            text=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n",
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = sessions(client)
        await session.build()
    result = json.loads((session.metadata / "result.json").read_text())
    assert session.generation == "submitted"
    assert session.workspace.read("main.py") == "print(42)\n"
    assert session.descriptor["entry"] == "main.py"
    assert session.dispatcher.tool_usage == {"calls": 2, "failures": 0}
    expected_tokens = sum(response["usage"]["total_tokens"] for response in responses)
    assert result["usage"]["total_tokens"] == expected_tokens
    assert result["harness"]["budget"]["used_tokens"] == expected_tokens
    assert len(result["harness"]["turns"]) == len(requests) == 3
    assert result["failure"] is None
    assert result["harness"]["recoveries"] == [{"kind": recovery, "phase": "building", "turn": 2}]
    if recovery == "empty_provider_retry":
        assert requests[1] == requests[2]
        assert result["harness"]["turns"][1]["failure"]["code"] == "provider_stream_error"


@pytest.mark.parametrize(
    "partial,code,build_turns,expected_requests",
    [
        (False, None, 32, 2),
        (False, 503, 32, 2),
        (False, 503, 1, 1),
        (True, 503, 32, 1),
        (False, 400, 32, 1),
    ],
)
async def test_provider_retry_is_bounded_and_never_replays_partial_output(
    sessions, monkeypatch, partial, code, build_turns, expected_requests
):
    requests = 0

    async def handler(request):
        nonlocal requests
        requests += 1
        prefix = ""
        if partial:
            payload = tool_response({"command": "write", "path": "bad.py", "content": "bad"}, "bad")
            prefix = f"data: {json.dumps(payload)}\n\n"
        failure = {"error": {"code": code, "message": "private"}}
        return web.Response(
            text=prefix + f"data: {json.dumps(failure)}\n\n", content_type="text/event-stream"
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = sessions(client, build_turns=build_turns)
        await session.build()
    result = session.result()
    assert requests == expected_requests == len(session.turns)
    assert result["failure"]["code"] == "provider_stream_error"
    assert result["failure"]["summary"] == "Provider failed during response"
    assert session.dispatcher.tool_usage["calls"] == 0
    assert session.workspace.ls() == []
    if not partial:
        assert result["usage"]["total_tokens"] is None
        assert result["harness"]["budget"]["estimated"] is True
        assert session.budget_tokens > 0


@pytest.mark.parametrize("recovery", ["text", "provider_error"])
async def test_recovery_cannot_bypass_total_token_budget(sessions, monkeypatch, recovery):
    requests = 0

    async def handler(request):
        nonlocal requests
        requests += 1
        payload = {"usage": {"prompt_tokens": 49999, "completion_tokens": 0, "total_tokens": 49999}}
        if recovery == "text":
            payload["choices"] = [{"delta": {"content": "Done"}, "finish_reason": "stop"}]
        else:
            payload["error"] = {"code": 503}
        return web.Response(
            text=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n",
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = sessions(client, total_tokens=50000)
        await session.build()
    assert requests == 1
    assert session.result()["failure"]["category"] == "token_budget"
    assert session.budget_tokens == 49999
    assert session.attempts == []


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
