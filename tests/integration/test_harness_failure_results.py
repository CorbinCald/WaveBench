from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from wavebench import api
from wavebench.harness import session as module
from wavebench.harness.config import Limits
from wavebench.harness.session import HarnessSession
from wavebench.harness.workspace import allocate_run
from wavebench.tokens import prompt_tokens

from ..harness_calls import tool_call


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

    def create(client=None, model="fixture/model", effort=None, **limits):
        session = HarnessSession(
            run,
            len(created) + 1,
            "Fixture",
            model,
            "List the empty workspace.",
            client,
            "offline-key",
            Limits(**limits),
            asyncio.Semaphore(1),
            asyncio.Semaphore(1),
            auto_open="off",
            reasoning_effort=effort,
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
    assert result["usage"]["total_tokens"] == 13
    assert result["usage"]["cost"] is None
    assert session.dispatcher.tool_usage["calls"] == 0
    assert requests == 1


@pytest.mark.parametrize("output_limit", [64_000, 32_000])
async def test_finishing_requests_keep_full_output_and_reasoning(
    sessions, monkeypatch, output_limit
):
    """Finishing narrows the work, never the response: a whole file still fits."""
    requests = []
    monkeypatch.setitem(api._MODEL_MAX_COMPLETION_CACHE, "fixture/model", output_limit)

    async def handler(request):
        data = await request.json()
        requests.append(data)
        assert data["max_tokens"] == output_limit
        assert data["reasoning"] == {"effort": "high"}
        assert "Finish now: make only essential fixes" in json.dumps(data["messages"])
        if len(requests) == 1:
            command = {
                "command": "write",
                "path": "main.py",
                "content": "# " + "code " * 6000 + "\nprint(42)\n",
            }
        else:
            assert data["messages"][-1]["content"] == "Wrote main.py (2 lines)."
            command = {"command": "done", "runtime": "python", "entry": "main.py"}
        payload = tool_response(command, f"complete-{len(requests)}")
        payload["choices"][0]["delta"]["reasoning"] = "Check the file and complete the tool call."
        incoming = prompt_tokens(data["messages"], data["tools"])
        outgoing = 22_000 if len(requests) == 1 else 100
        payload["usage"] = {
            "prompt_tokens": incoming,
            "completion_tokens": outgoing,
            "total_tokens": incoming + outgoing,
            "completion_tokens_details": {"reasoning_tokens": 15_000 if len(requests) == 1 else 50},
        }
        return web.Response(
            text=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n",
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = sessions(client, effort="high", build_turns=3)
        await session.build()
    assert session.generation == "submitted", session.error
    assert session.workspace.read("main.py").endswith("print(42)\n")
    assert len(requests) == 2 and session.dispatcher.tool_usage == {"calls": 2, "failures": 0}
    assert session.notices[0]["kind"] == "finishing" and session.recoveries == []


@pytest.mark.parametrize(
    "failure,reported",
    [
        ("length", True),
        ("truncated_arguments", True),
        ("invalid_arguments", True),
        ("batch", True),
        ("batch", False),
    ],
)
@pytest.mark.parametrize("persistent", [False, True])
async def test_bad_response_retries_once_without_executing_any_failed_calls(
    sessions, monkeypatch, failure, reported, persistent
):
    requests = []

    async def handler(request):
        data = await request.json()
        requests.append(data)
        if len(requests) == 1 or persistent:
            payload = tool_response(
                {"command": "write", "path": "rejected.py", "content": "print('must not run')"},
                "rejected",
            )
            calls = payload["choices"][0]["delta"]["tool_calls"]
            if failure == "length":
                payload["choices"][0]["finish_reason"] = "length"
            elif failure in {"truncated_arguments", "invalid_arguments"}:
                calls.append(
                    {
                        "index": 1,
                        "id": "broken",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path":"x.py","content":"unfinished',
                        },
                    }
                )
            else:
                calls.extend(
                    {
                        "index": n,
                        "id": f"extra-{n}",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": "{}"},
                    }
                    for n in range(1, 65)
                )
            outgoing = data["max_tokens"] if failure in {"length", "truncated_arguments"} else 100
        else:
            assert "[WaveBench] Your last response" in json.dumps(data["messages"])
            assert "rejected.py" not in json.dumps(data["messages"])
            if len(requests) == 2:
                assert not session.workspace.ls()
            payload = (
                tool_response(
                    {"command": "write", "path": "main.py", "content": "print(42)\n"}, "write-ok"
                )
                if len(requests) == 2
                else tool_response(
                    {"command": "done", "runtime": "python", "entry": "main.py"}, "submit-ok"
                )
            )
            outgoing = 100
        if reported or (len(requests) > 1 and not persistent):
            payload["usage"] = {
                "prompt_tokens": 1000,
                "completion_tokens": outgoing,
                "total_tokens": 1000 + outgoing,
            }
        else:
            payload.pop("usage", None)
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
    # A persistent failure is retried at most three times per phase.
    assert len(requests) == (4 if persistent else 3)
    assert len(session.recoveries) == (3 if persistent else 1)
    if not reported:
        assert session.usage()["total_tokens"] is None
    assert "rejected.py" not in str(session.workspace.ls())
    first = session.turns[0]
    assert first["failure"]["code"] == (
        "tool_batch_limit"
        if failure == "batch"
        else "invalid_tool_arguments"
        if failure == "invalid_arguments"
        else "output_truncated"
    )
    if failure == "batch":
        assert first["stream"]["policy"]["tool_calls"] == 64
        assert first["stream"]["parsing"]["tool_calls"] == 64
    if persistent:
        assert session.generation == "failed" and session.dispatcher.tool_usage["calls"] == 0
    else:
        assert session.generation == "submitted", session.error
        assert session.dispatcher.tool_usage == {"calls": 2, "failures": 0}


@pytest.mark.parametrize("phase_expires_first", [False, True])
async def test_header_wait_failure_is_saved_retried_and_bounded_by_the_phase(
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
            response_headers_seconds=3 if phase_expires_first else 2,
            build_seconds=1 if phase_expires_first else 3,
        )
        try:
            await asyncio.wait_for(session.build(), 5)
        finally:
            release.set()
    result = json.loads((session.metadata / "result.json").read_text())
    # Either way the phase's time limit ends the run; a header timeout is retried once first.
    assert result["failure"]["code"] == "time_or_turn_limit"
    turns = result["harness"]["turns"]
    assert all(turn["stream"]["stage"] == "response_headers" for turn in turns)
    assert all(turn["usage"] == {} for turn in turns)
    assert result["usage"]["total_tokens"] is None and result["usage"]["cost"] is None
    if phase_expires_first:
        assert requests == 1 and turns[0]["stream"]["failure_code"] == "request_cancelled"
        assert result["harness"]["recoveries"] == []
    else:
        assert requests == 2
        assert turns[0]["failure"]["summary"] == "No response headers received"
        assert turns[0]["stream"]["policy"] == {"response_headers_seconds": 2}
        assert result["harness"]["recoveries"] == [
            {
                "kind": "provider_retry",
                "phase": "building",
                "turn": 1,
                "failure_code": "response_headers_timeout",
            }
        ]
    assert result["retries"] == [] and session.dispatcher.tool_usage["calls"] == 0


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
        assert "No project has been submitted" in requests[1]["messages"][-1]["content"]


def tool_response(command, call_id):
    return {
        "choices": [
            {
                "delta": {"tool_calls": [tool_call(call_id, command)]},
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
    }


@pytest.mark.parametrize("recovery", ["submission_reminder", "empty_error", "reasoning_error"])
async def test_recovery_preserves_project_and_accounts_for_every_request(
    sessions, monkeypatch, recovery
):
    requests = []
    write = tool_response(
        {"command": "write", "path": "main.py", "content": "print(42)\n"}, "write"
    )
    interrupted = {
        "submission_reminder": [
            {
                "choices": [{"delta": {"content": "Implemented; ready."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
            }
        ],
        "empty_error": [
            {
                "error": {"code": 503, "message": "private provider body"},
                "usage": {"prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100},
            }
        ],
        # The Grok 4.7 failure: reasoning streamed, then the provider returned 502.
        "reasoning_error": [
            {"choices": [{"delta": {"reasoning": "Plan the level, then write it."}}]},
            {
                "error": {"code": 502, "message": "private provider body"},
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 12,
                    "total_tokens": 112,
                    "completion_tokens_details": {"reasoning_tokens": 12},
                },
            },
        ],
    }[recovery]
    responses = [
        [write],
        interrupted,
        [tool_response({"command": "done", "runtime": "python", "entry": "main.py"}, "done")],
    ]

    async def handler(request):
        requests.append(await request.json())
        events = "".join(f"data: {json.dumps(p)}\n\n" for p in responses[len(requests) - 1])
        return web.Response(text=events + "data: [DONE]\n\n", content_type="text/event-stream")

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
    expected_tokens = sum(events[-1]["usage"]["total_tokens"] for events in responses)
    assert result["usage"]["total_tokens"] == expected_tokens
    assert len(result["harness"]["turns"]) == len(requests) == 3
    assert result["failure"] is None
    expected = {"kind": "submission_reminder", "phase": "building", "turn": 2}
    if recovery != "submission_reminder":
        expected = {**expected, "kind": "provider_retry", "failure_code": "provider_stream_error"}
    assert result["harness"]["recoveries"] == [expected]
    if recovery != "submission_reminder":
        # The retry resends the unchanged conversation; partial reasoning is discarded.
        assert requests[1] == requests[2]
        assert "Plan the level" not in json.dumps(requests[2])
        assert result["harness"]["turns"][1]["failure"]["code"] == "provider_stream_error"


@pytest.mark.parametrize(
    "partial,code,build_turns,expected_requests",
    [
        (None, None, 32, 4),
        (None, 503, 32, 4),
        (None, 503, 1, 1),
        # Discarded partial output is never executed or replayed, so it can be retried.
        ("tool", 503, 32, 4),
        ("reasoning", 502, 32, 4),
        ("reasoning", 502, 1, 1),
        # A rejected request would only be rejected again.
        (None, 400, 32, 1),
        ("reasoning", 400, 32, 1),
    ],
)
async def test_provider_retry_is_bounded_and_never_replays_partial_output(
    sessions, monkeypatch, partial, code, build_turns, expected_requests
):
    requests = 0

    bodies = []

    async def handler(request):
        nonlocal requests
        requests += 1
        bodies.append(await request.json())
        prefix = ""
        if partial == "tool":
            payload = tool_response({"command": "write", "path": "bad.py", "content": "bad"}, "bad")
            prefix = f"data: {json.dumps(payload)}\n\n"
        elif partial == "reasoning":
            payload = {"choices": [{"delta": {"reasoning": "Plan first."}}]}
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
    assert all(body == bodies[0] for body in bodies)  # The same conversation each time.
    if not partial:
        assert result["usage"]["total_tokens"] is None


async def test_reasoning_that_fills_the_output_steps_the_effort_down(sessions, monkeypatch):
    """The Claude Opus 5.5 failure: at max effort, a turn spent all 64,000 tokens reasoning."""
    model = "anthropic/claude-opus-5.5"
    monkeypatch.setitem(api._MODEL_CONTEXT_CACHE, model, 1_000_000)
    efforts = []

    async def handler(request):
        data = await request.json()
        efforts.append(data["reasoning"]["effort"])
        if len(efforts) == 1:
            payload = {
                "choices": [{"delta": {"reasoning": "thinking " * 50}, "finish_reason": "length"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 64_000,
                    "total_tokens": 64_100,
                },
            }
        elif len(efforts) == 2:
            payload = tool_response(
                {"command": "write", "path": "main.py", "content": "print(1)\n"}, "w"
            )
        else:
            payload = tool_response(
                {"command": "done", "runtime": "python", "entry": "main.py"}, "d"
            )
        return web.Response(
            text=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n",
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = sessions(client, model=model, effort="max")
        await session.build()
    assert session.generation == "submitted", session.error
    assert efforts == ["max", "xhigh", "xhigh"]
    recovery = session.recoveries[0]
    assert recovery["failure_code"] == "output_truncated"
    assert recovery["reasoning_effort"] == {"from": "max", "to": "xhigh"}
    assert session.result()["harness"]["reasoning_effort"] == {
        "configured": "max",
        "final": "xhigh",
    }


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
