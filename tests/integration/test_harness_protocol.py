from __future__ import annotations

import asyncio
import copy
import json

import aiohttp
import pytest
from aiohttp import web

from wavebench import api
from wavebench.harness.commands import TOOL_SCHEMA
from wavebench.harness.transport import StreamAssembly, TurnError, call_conversation
from wavebench.prompt_cache import CachePolicy


def feed(assembly, delta, **extra):
    assembly.feed(
        json.dumps(
            {
                "model": "actual/model",
                "provider": "Actual provider",
                "choices": [{"index": 0, "delta": delta, **extra}],
            }
        )
    )


@pytest.mark.parametrize(
    "model,field",
    [
        ("openai/gpt-5.6-luna", "prompt_cache_breakpoint"),
        ("openai/gpt-6-astra", "prompt_cache_breakpoint"),
        ("anthropic/claude-haiku-4.5", "cache_control"),
        ("google/gemini-2.5-flash", "cache_control"),
    ],
)
async def test_cache_wire_payload_is_stable_across_http_retry(monkeypatch, model, field):
    requests = []

    async def handler(request):
        requests.append(await request.json())
        if len(requests) == 1:
            return web.Response(status=503, text="retry", headers={"Retry-After": "0"})
        return web.Response(
            text='data: {"model":"'
            + model
            + '","choices":[{"delta":{"content":"complete"},"finish_reason":"stop"}],"usage":{"prompt_tokens":10000,"completion_tokens":10,"total_tokens":10010,"prompt_tokens_details":{"cached_tokens":9000,"cache_write_tokens":1000}}}\n\ndata: [DONE]\n\n',
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(
        api, "API_URL", f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    )
    monkeypatch.setattr(api, "_MODEL_CONTEXTS_ATTEMPTED", True)
    messages = [{"role": "user", "content": "reference data " * 5000}]
    original = copy.deepcopy(messages)
    policy = CachePolicy(model)
    try:
        async with aiohttp.ClientSession() as client:
            turn = await call_conversation(
                client,
                "offline",
                model,
                messages,
                TOOL_SCHEMA,
                max_tokens=1024,
                reasoning_effort=None,
                input_tokens_bound=11000,
                cache_policy=policy,
            )
        assert requests[0] == requests[1]
        assert requests[0]["tools"] == TOOL_SCHEMA
        assert field in requests[0]["messages"][0]["content"][0]
        if model.startswith("openai/"):
            assert requests[0]["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
        assert messages == original
        assert turn.usage["prompt_tokens_details"]["cached_tokens"] == 9000
        assert turn.adjustments["cache"]["session_id"] == policy.key
    finally:
        await runner.cleanup()


async def test_compaction_high_effort_never_negotiates_down(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(await request.json())
        return web.Response(status=400, text="unsupported reasoning effort")

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(
        api, "API_URL", f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    )
    monkeypatch.setattr(api, "_MODEL_CONTEXTS_ATTEMPTED", True)
    try:
        async with aiohttp.ClientSession() as client:
            with pytest.raises(TurnError, match="reasoning"):
                await call_conversation(
                    client,
                    "offline",
                    "openai/gpt-5.6-luna",
                    [{"role": "user", "content": "Summarize"}],
                    [],
                    max_tokens=1024,
                    reasoning_effort="high",
                    strict_reasoning=True,
                    cache_reuse=False,
                )
        assert len(requests) == 1
        assert requests[0]["reasoning"] == {"effort": "high"}
        assert "tools" not in requests[0]
        assert requests[0]["prompt_cache_options"]["mode"] == "explicit"
        assert "prompt_cache_breakpoint" not in json.dumps(requests[0]["messages"])
    finally:
        await runner.cleanup()


def test_fragments_ids_reasoning_signatures_and_provider_fields():
    assembly = StreamAssembly()
    feed(
        assembly,
        {
            "reasoning_details": [
                {"index": 0, "type": "reasoning.encrypted", "data": "abc", "format": "provider"}
            ],
            "tool_calls": [
                {
                    "index": 0,
                    "id": "a",
                    "type": "function",
                    "function": {"name": "wb", "arguments": '{"command":"write",'},
                }
            ],
        },
    )
    feed(
        assembly,
        {
            "reasoning_details": [{"index": 0, "type": "reasoning.encrypted", "data": "def"}],
            "tool_calls": [
                {"index": 0, "function": {"arguments": '"path":"main.py","content":"print(42)"}'}}
            ],
            "extra_content": {"google": {"thought_signature": "signature"}},
        },
    )
    feed(assembly, {}, finish_reason="tool_calls")
    assembly.feed(json.dumps({"usage": {"total_tokens": 42}}))
    assembly.feed("[DONE]")
    turn = assembly.complete()
    assert (
        json.loads(turn.message["tool_calls"][0]["function"]["arguments"])["content"] == "print(42)"
    )
    assert turn.message["reasoning_details"][0]["data"] == "abcdef"
    assert turn.message["extra_content"]["google"]["thought_signature"] == "signature"
    assert turn.provider == "Actual provider" and turn.usage["total_tokens"] == 42


@pytest.mark.parametrize(
    "finish,done,arguments",
    [("tool_calls", False, "{}"), ("length", True, "{}"), ("tool_calls", True, '{"command":')],
)
def test_partial_or_truncated_calls_never_complete(finish, done, arguments):
    assembly = StreamAssembly()
    feed(
        assembly,
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call",
                    "type": "function",
                    "function": {"name": "wb", "arguments": arguments},
                }
            ]
        },
        finish_reason=finish,
    )
    if done:
        assembly.feed("[DONE]")
    with pytest.raises(TurnError, match="no tools executed"):
        assembly.complete()


@pytest.mark.parametrize("model", ["vendor/model", "openai/gpt-6-astra"])
async def test_real_http_retry_utf8_stream_and_second_conversation_request(monkeypatch, model):
    requests = []
    retries = []

    async def handler(request):
        data = await request.json()
        requests.append(data)
        if len(requests) == 1:
            return web.Response(status=503, headers={"Retry-After": "0"}, text="retry")
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        objects = [
            {
                "model": "provider/model",
                "provider": "family",
                "choices": [
                    {
                        "delta": {
                            "content": "café 🎉",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-one",
                                    "type": "function",
                                    "function": {"name": "wb", "arguments": '{"command":"ls"}'},
                                }
                            ],
                        }
                    }
                ],
            },
            {
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            },
        ]
        payload = (
            "".join("data: " + json.dumps(obj, ensure_ascii=False) + "\r\n\r\n" for obj in objects)
            + "data: [DONE]\n\n"
        )
        raw = payload.encode()
        for i in range(0, len(raw), 7):
            await response.write(raw[i : i + 7])
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(api, "API_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(api, "_MODEL_CONTEXTS_ATTEMPTED", True)
    messages = [{"role": "user", "content": "build"}]
    policy = CachePolicy(model)
    try:
        async with aiohttp.ClientSession() as session:
            turn = await call_conversation(
                session,
                "test-key",
                model,
                messages,
                TOOL_SCHEMA,
                max_tokens=100,
                reasoning_effort="low",
                cache_policy=policy,
                on_retry=lambda *args: retries.append(args),
            )
            assert turn.message["content"] == "café 🎉"
            assert len(retries) == 1 and retries[0][0] == 503
            messages.extend(
                [
                    turn.message,
                    {"role": "tool", "tool_call_id": "call-one", "content": '{"ok":true}'},
                ]
            )
            # The unchanged conversation prefix has a measured token count;
            # its JSON byte length alone would reject this smaller context.
            monkeypatch.setitem(api._MODEL_CONTEXT_CACHE, model, 1200)
            second_turn = await call_conversation(
                session,
                "test-key",
                model,
                messages,
                TOOL_SCHEMA,
                max_tokens=100,
                reasoning_effort="low",
                cache_policy=policy,
                input_tokens_bound=50,
            )
            assert second_turn.adjustments["context_bound"] == 1074
        assert len(requests) == 3
        assert all(request["tools"] == TOOL_SCHEMA for request in requests)
        assert requests[0] == requests[1]
        assert requests[-1]["messages"][-2] == turn.message
        assert requests[-1]["messages"][-1]["tool_call_id"] == "call-one"
        assert requests[-1]["model"] == model
        if model.startswith("openai/"):
            assert all(
                request["prompt_cache_options"]["mode"] == "implicit" for request in requests
            )
            assert requests[-1]["prompt_cache_key"] == requests[0]["prompt_cache_key"]
            assert requests[-1]["messages"][-1] == messages[-1]
    finally:
        await runner.cleanup()


async def test_context_exhaustion_is_local(monkeypatch):
    monkeypatch.setattr(api, "_MODEL_CONTEXTS_ATTEMPTED", True)
    monkeypatch.setitem(api._MODEL_CONTEXT_CACHE, "tiny", 100)
    with pytest.raises(TurnError, match="context budget"):
        await call_conversation(
            None,
            "key",
            "tiny",
            [{"role": "user", "content": "x" * 200}],
            TOOL_SCHEMA,
            max_tokens=100,
            reasoning_effort=None,
        )


async def test_concurrent_catalog_preflight_waits_for_capabilities(monkeypatch):
    class Response:
        status = 200

        async def __aenter__(self):
            await asyncio.sleep(0.02)
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return {
                "data": [
                    {
                        "id": "test/no-tools",
                        "supported_parameters": ["max_tokens"],
                        "context_length": 4096,
                    }
                ]
            }

    class Client:
        calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return Response()

    monkeypatch.setattr(api, "_MODEL_CONTEXTS_ATTEMPTED", False)
    monkeypatch.setattr(api, "_MODEL_CONTEXT_LOCK", asyncio.Lock())
    monkeypatch.setattr(api, "_MODEL_TOOL_CACHE", {})
    client = Client()

    async def check():
        await api._load_model_context_lengths(client, "offline")
        return api._MODEL_TOOL_CACHE.get("test/no-tools")

    assert await asyncio.gather(check(), check()) == [False, False]
    assert client.calls == 1
