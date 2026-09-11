from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from wavebench import api
from wavebench.harness import session as session_module
from wavebench.harness.config import Limits
from wavebench.harness.failure import failure_record
from wavebench.harness.session import HarnessSession
from wavebench.harness.transport import TurnError, call_conversation
from wavebench.harness.workspace import allocate_run

MODEL = "google/gemini-3.8-flash"
SIGNATURE = {
    "index": 0,
    "type": "reasoning.encrypted",
    "format": "google-gemini-v1",
    "id": "write",
    "data": "opaque-signed-tool-history",
}


def sse(payload):
    return web.Response(
        text=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n", content_type="text/event-stream"
    )


def tool(provider, command, call_id):
    return {
        "provider": provider,
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
                    ],
                    "reasoning_details": [{**SIGNATURE, "id": call_id}],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
    }


@pytest.fixture
async def factory(tmp_path, monkeypatch):
    async def preflight(self):
        pass  # Provider protocol tests stop before program execution.

    monkeypatch.setattr(session_module.Runtime, "preflight", preflight)
    monkeypatch.setattr(api, "_MODEL_CONTEXTS_ATTEMPTED", True)
    monkeypatch.setitem(api._MODEL_CONTEXT_CACHE, MODEL, 1_000_000)
    run = allocate_run(tmp_path, "provider-continuity", "fixture")
    sessions = []

    def create(client, model=MODEL):
        session = HarnessSession(
            run,
            len(sessions) + 1,
            "Fixture",
            model,
            "Write main.py and submit.",
            client,
            "offline-key",
            Limits(),
            asyncio.Semaphore(1),
            asyncio.Semaphore(1),
            auto_open="off",
            reasoning_effort=None,
        )
        sessions.append(session)
        return session

    yield create
    for session in sessions:
        await session.close()


@pytest.mark.parametrize(
    "provider,slug", [("Google", "google-vertex"), ("Google AI Studio", "google-ai-studio")]
)
async def test_gemini_keeps_provider_and_signatures_through_http_and_stream_retries(
    factory, monkeypatch, provider, slug
):
    requests = []

    async def handler(request):
        requests.append(await request.json())
        index = len(requests)
        if index == 1:
            return sse(
                tool(
                    provider,
                    {"command": "write", "path": "main.py", "content": "print(42)"},
                    "write",
                )
            )
        if index == 2:
            return web.Response(status=503, text="busy", headers={"Retry-After": "0"})
        if index == 3:
            return sse(tool(provider, {"command": "ls"}, "list"))
        if index == 4:
            return sse(
                {
                    "provider": provider,
                    "error": {"code": 503},
                    "usage": {"prompt_tokens": 100, "total_tokens": 100},
                }
            )
        return sse(
            tool(provider, {"command": "done", "runtime": "python", "entry": "main.py"}, "done")
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = factory(client)
        await session.build()
    assert session.generation == "submitted"
    assert len(requests) == 5 and len(session.turns) == 4
    assert session.budget_tokens == 430
    assert session.dispatcher.tool_usage == {"calls": 3, "failures": 0}
    assert session.result()["harness"]["gemini_provider"] == provider
    assert requests[0]["provider"] == {"require_parameters": True}
    for request in requests[1:]:
        assert request["provider"] == {
            "require_parameters": True,
            "only": [slug],
            "allow_fallbacks": False,
        }
        assert request["messages"][2]["reasoning_details"] == [SIGNATURE]
    assert requests[1] == requests[2]
    assert requests[3] == requests[4]
    assert session.turns[-1]["adjustments"]["provider_routing"] == requests[-1]["provider"]


@pytest.mark.parametrize("provider", [None, "Unknown provider", "Google AI Studio"])
async def test_provider_mismatch_does_not_execute_tools_or_change_binding(
    factory, monkeypatch, provider
):
    requests = []

    async def handler(request):
        requests.append(await request.json())
        first = len(requests) == 1
        return sse(
            tool(
                "Google" if first else provider,
                {
                    "command": "write",
                    "path": "main.py" if first else "wrong.py",
                    "content": "print(42)",
                },
                "write" if first else "wrong",
            )
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = factory(client)
        await session.build()
    assert len(requests) == 2
    assert session.result()["failure"]["code"] == "provider_changed"
    assert session.gemini_provider == "Google"
    assert session.dispatcher.tool_usage["calls"] == 1
    assert not (session.workspace.root / "wrong.py").exists()
    assert session.budget_tokens == 220
    assert session.result()["failure"]["diagnostics"]["pinned_provider"] == "Google"


@pytest.mark.parametrize("provider", [None, "private prompt"])
async def test_unknown_initial_gemini_provider_cannot_start_tool_history(
    factory, monkeypatch, provider
):
    async def handler(request):
        return sse(
            tool(provider, {"command": "write", "path": "main.py", "content": "print(42)"}, "write")
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = factory(client)
        await session.build()
    assert session.result()["failure"]["code"] == "provider_identity_missing"
    assert session.dispatcher.tool_usage["calls"] == 0
    assert session.budget_tokens == 110
    assert "private prompt" not in json.dumps(session.result()["failure"])


async def test_unavailable_bound_provider_cannot_fall_back(factory, monkeypatch):
    requests = []
    monkeypatch.setattr(api, "_MAX_RETRIES", 1)

    async def handler(request):
        requests.append(await request.json())
        if len(requests) == 1:
            return sse(tool("Google", {"command": "ls"}, "list"))
        return web.Response(status=503, text="unavailable", headers={"Retry-After": "0"})

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = factory(client)
        await session.build()
    assert len(requests) == 3
    assert all(
        r["provider"].get("only") == ["google-vertex"] and r["provider"]["allow_fallbacks"] is False
        for r in requests[1:]
    )
    assert session.gemini_provider == "Google"
    assert session.result()["failure"]["code"] == "http_error"
    assert session.dispatcher.tool_usage["calls"] == 1


async def test_compaction_and_repair_preserve_binding_but_compactor_is_unrestricted(
    factory, monkeypatch
):
    requests = []

    async def handler(request):
        data = await request.json()
        requests.append(data)
        if data["model"] == session_module.COMPACTION_MODEL:
            return sse(
                {
                    "model": session_module.COMPACTION_MODEL,
                    "provider": "OpenAI",
                    "choices": [
                        {"delta": {"content": "Project file exists."}, "finish_reason": "stop"}
                    ],
                    "usage": {"total_tokens": 100},
                }
            )
        command = {"command": "done", "runtime": "python", "entry": "main.py"}
        if len(requests) == 1:
            command = {"command": "write", "path": "main.py", "content": "print(42)"}
        return sse(tool("Google", command, f"call-{len(requests)}"))

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = factory(client)
        await session.build()
        session.messages.insert(
            2, {"role": "assistant", "content": "old reference material " * 5000}
        )
        assert await session.compact("context exceeded 240,000 tokens", 50000, 10)
        session.messages.append({"role": "user", "content": "First run failed; repair and submit."})
        session.dispatcher.submission = None
        await session.conversation(repair=True)
    assert [r["model"] for r in requests] == [MODEL, MODEL, session_module.COMPACTION_MODEL, MODEL]
    assert requests[2]["provider"] == {"require_parameters": True}
    assert requests[3]["provider"]["only"] == ["google-vertex"]
    assert session.gemini_provider == "Google"
    assert session.turns[-1]["phase"] == "repairing"
    assert session.dispatcher.submission["entry"] == "main.py"


async def test_other_models_keep_automatic_provider_routing(factory, monkeypatch):
    requests = []

    async def handler(request):
        requests.append(await request.json())
        first = len(requests) == 1
        command = (
            {"command": "write", "path": "main.py", "content": "print(42)"}
            if first
            else {"command": "done", "runtime": "python", "entry": "main.py"}
        )
        return sse(tool("OpenAI" if first else "Azure", command, str(len(requests))))

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        session = factory(client, model="openai/fixture")
        await session.build()
    assert session.generation == "submitted" and session.gemini_provider is None
    assert all(r["provider"] == {"require_parameters": True} for r in requests)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "message",
    [
        "Corrupted thought signature.",
        "Function call is missing a thought_signature; preserve reasoning details.",
    ],
)
async def test_signature_rejection_is_specific_private_and_never_retried(
    factory, monkeypatch, stream, message
):
    requests = []
    error = {
        "code": 400,
        "message": "Provider returned error",
        "metadata": {
            "raw": json.dumps({"error": {"message": message + " private prompt offline-key"}}),
            "provider_name": "Google AI Studio",
        },
    }

    async def handler(request):
        requests.append(await request.json())
        return (
            sse({"provider": "Google AI Studio", "error": error, "usage": {"total_tokens": 20}})
            if stream
            else web.json_response({"error": error}, status=400)
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        monkeypatch.setattr(api, "API_URL", str(server.make_url("")))
        with pytest.raises(TurnError) as caught:
            await call_conversation(
                client,
                "offline-key",
                MODEL,
                [{"role": "user", "content": "private prompt"}],
                [],
                max_tokens=256,
                reasoning_effort="low",
                gemini_provider="Google AI Studio",
            )
    failure = failure_record(caught.value)
    assert len(requests) == 1
    assert failure["code"] == "thought_signature_invalid"
    assert failure["summary"] == "Gemini thought signature rejected"
    assert "private prompt" not in json.dumps(failure)
    assert "offline-key" not in json.dumps(failure)
    if stream:
        assert caught.value.usage["total_tokens"] == 20
        assert caught.value.diagnostics["provider_error"]["retryable_empty_response"] is False
