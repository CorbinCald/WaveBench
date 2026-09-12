from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace

import aiohttp
import pytest
from aiohttp import web

from wavebench import api
from wavebench.harness.config import Limits
from wavebench.harness.transport import StreamPolicy, StreamReader, TurnError, call_conversation
from wavebench.tokens import count_tokens


def event(delta=None, **fields):
    return (
        "data: "
        + json.dumps({"choices": [{"delta": delta or {}, **fields}]}, ensure_ascii=False)
        + "\n\n"
    ).encode()


@asynccontextmanager
async def server(monkeypatch, handler):
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
            yield client
    finally:
        await runner.cleanup()


async def conversation(client, **kwargs):
    return await call_conversation(
        client,
        "sk-secret-local",
        "test/model",
        [{"role": "user", "content": "private prompt that must not reach diagnostics"}],
        [],
        max_tokens=64_000,
        reasoning_effort=None,
        **kwargs,
    )


@pytest.mark.parametrize("reject_first", [False, True])
async def test_missing_headers_times_out_without_replay_and_releases_connection(
    monkeypatch, reject_first
):
    release = asyncio.Event()
    diagnostics = []
    requests = 0

    async def handler(request):
        nonlocal requests
        requests += 1
        if reject_first and requests == 1:
            return web.Response(status=503, headers={"Retry-After": "0"})
        await release.wait()
        return web.Response(
            body=event({"content": "complete"}, finish_reason="stop") + b"data: [DONE]\n\n",
            content_type="text/event-stream",
        )

    limits = replace(Limits(), response_headers_seconds=1)
    async with server(monkeypatch, handler):
        # A leaked connection after timeout would block the next request.
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=1)) as client:
            try:
                with pytest.raises(TurnError) as error:
                    await asyncio.wait_for(
                        conversation(
                            client, stream_limits=limits, on_diagnostics=diagnostics.append
                        ),
                        3,
                    )
                assert requests == 1 + int(reject_first)
            finally:
                release.set()
            turn = await asyncio.wait_for(conversation(client, stream_limits=limits), 2)
    assert turn.message["content"] == "complete"
    assert error.value.failure_code == "response_headers_timeout"
    assert error.value.request_sent and error.value.usage == {}
    assert diagnostics == [error.value.diagnostics]
    assert diagnostics[0]["stage"] == "response_headers"
    assert diagnostics[0]["policy"] == {"response_headers_seconds": 1}
    assert 1 <= diagnostics[0]["elapsed_seconds"] < 2
    assert "private prompt" not in str(error.value) + json.dumps(diagnostics)
    assert "sk-secret-local" not in str(error.value) + json.dumps(diagnostics)


async def test_tls_connection_stall_obeys_response_header_deadline(monkeypatch):
    closed = asyncio.Event()

    async def handler(reader, writer):
        try:
            # Accept TCP but never answer the client's TLS handshake.
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    async with await asyncio.start_server(handler, "127.0.0.1", 0) as listener:
        monkeypatch.setattr(
            api, "API_URL", f"https://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        )
        monkeypatch.setattr(api, "_MODEL_CONTEXTS_ATTEMPTED", True)
        async with aiohttp.ClientSession() as client:
            with pytest.raises(TurnError) as error:
                await asyncio.wait_for(
                    conversation(
                        client, stream_limits=replace(Limits(), response_headers_seconds=1)
                    ),
                    2,
                )
        await asyncio.wait_for(closed.wait(), 2)
    assert error.value.failure_code == "response_headers_timeout"


async def test_stream_can_outlive_response_header_deadline(monkeypatch):
    async def handler(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for _ in range(6):
            await response.write(event({"content": "working "}))
            await asyncio.sleep(0.25)
        await response.write(
            event(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "one",
                            "type": "function",
                            "function": {"name": "wb", "arguments": '{"command":"ls"}'},
                        }
                    ]
                },
                finish_reason="tool_calls",
            )
        )
        await response.write(b"data: [DONE]\n\n")
        return response

    limits = replace(Limits(), response_headers_seconds=1, stream_seconds=3, stream_idle_seconds=1)
    async with server(monkeypatch, handler) as client:
        started = time.monotonic()
        turn = await asyncio.wait_for(conversation(client, stream_limits=limits), 4)
    assert time.monotonic() - started > limits.response_headers_seconds
    assert turn.message["tool_calls"][0]["function"]["arguments"] == '{"command":"ls"}'
    assert turn.adjustments["response_headers"]["limit_seconds"] == 1
    assert turn.adjustments["response_headers"]["elapsed_seconds"] < 1
    assert turn.adjustments["stream"]["parsing"]["done"]


async def test_cancellation_before_headers_stays_cancellation(monkeypatch):
    received = asyncio.Event()
    release = asyncio.Event()
    diagnostics = []

    async def handler(request):
        received.set()
        await release.wait()
        return web.Response()

    async with server(monkeypatch, handler) as client:
        task = asyncio.create_task(conversation(client, on_diagnostics=diagnostics.append))
        try:
            await asyncio.wait_for(received.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
    assert diagnostics[0]["failure_code"] == "request_cancelled"
    assert diagnostics[0]["stage"] == "response_headers"


async def test_rejected_response_body_is_bounded_and_not_exposed(monkeypatch):
    release = asyncio.Event()

    async def handler(request):
        response = web.StreamResponse(status=400)
        await response.prepare(request)
        await response.write(b"private prompt sk-secret-local " + b"oversized rejection " * 1000)
        # The client must reject using a bounded prefix without waiting for EOF.
        await release.wait()
        return response

    async with server(monkeypatch, handler) as client:
        try:
            with pytest.raises(TurnError) as error:
                await asyncio.wait_for(conversation(client), 2)
        finally:
            release.set()
    assert error.value.failure_code == "http_error"
    assert error.value.diagnostics == {"http_status": 400}
    assert "private prompt" not in str(error.value)
    assert "sk-secret-local" not in str(error.value)
    assert len(str(error.value)) < 100


async def test_malformed_event_in_same_read_preserves_partial_output_estimate(monkeypatch):
    text = "visible output " * 100
    updates = []

    async def handler(request):
        return web.Response(
            body=event({"content": text}) + b"data: {bad-json}\n\n",
            content_type="text/event-stream",
        )

    async with server(monkeypatch, handler) as client:
        with pytest.raises(TurnError):
            await conversation(
                client, on_usage=lambda usage, output: updates.append((usage, output))
            )
    assert updates[-1] == ({}, count_tokens(text))


def test_stream_policy_scales_is_bounded_and_is_recorded():
    limits = Limits()
    small = StreamPolicy.resolve(limits, 1024)
    large = StreamPolicy.resolve(limits, 64_000)
    huge = StreamPolicy.resolve(limits, 1_000_000_000)
    assert small.raw_bytes == 16 * 1024 * 1024
    assert large.raw_bytes == 64_000 * 1024
    assert large.output_bytes == 64_000 * 64
    assert huge.raw_bytes == limits.stream_raw_max_bytes
    assert huge.output_bytes == limits.stream_output_max_bytes
    assert Limits.from_config({"harness": limits.record()}) == limits
    with pytest.raises(ValueError, match="exceeds"):
        replace(limits, stream_raw_max_bytes=1)
    with pytest.raises(ValueError, match="positive integer"):
        replace(limits, stream_frame_bytes=0)


async def test_more_than_eight_mib_of_framing_can_finish_with_valid_tool(monkeypatch):
    requests = []
    heartbeat = b":" + b" framing" * 8190 + b"\n\n"
    tool = {
        "tool_calls": [
            {
                "index": 0,
                "id": "one",
                "type": "function",
                "function": {"name": "wb", "arguments": '{"command":"ls"}'},
            }
        ]
    }

    async def handler(request):
        requests.append(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for _ in range(132):
            await response.write(heartbeat)
        await response.write(event(tool, finish_reason="tool_calls"))
        await response.write(b"data: [DONE]\n\n")
        return response

    async with server(monkeypatch, handler) as client:
        turn = await conversation(client)
    stream = turn.adjustments["stream"]
    assert stream["bytes"]["raw"] > 8 * 1024 * 1024
    assert stream["bytes"]["content"] == 0
    assert stream["bytes"]["tool_arguments"] == len('{"command":"ls"}')
    assert stream["bytes"]["output"] == len('wb{"command":"ls"}')
    assert stream["policy"]["output_tokens"] == requests[0]["max_tokens"]
    assert turn.message["tool_calls"][0]["function"]["name"] == "wb"
    assert turn.usage == {}
    assert stream["provider_usage_available"] is False


@pytest.mark.parametrize(
    "payload,overrides,code,limit",
    [
        (
            event({"content": "private prompt secret"}),
            {"stream_output_min_bytes": 5, "stream_output_max_bytes": 5},
            "stream_output_limit",
            "output_bytes",
        ),
        (
            event({"reasoning": "oversized reasoning"}),
            {"stream_output_min_bytes": 5, "stream_output_max_bytes": 5},
            "stream_output_limit",
            "output_bytes",
        ),
        (
            b"data: " + b"x" * 1000,
            {"stream_frame_bytes": 100},
            "stream_frame_limit",
            "frame_bytes",
        ),
        (
            b"data: {\ndata: " + b" " * 200,
            {"stream_frame_bytes": 100},
            "stream_frame_limit",
            "frame_bytes",
        ),
        (
            b": " + b"x" * 200 + b"\n\n",
            {"stream_raw_min_bytes": 100, "stream_raw_max_bytes": 100},
            "stream_raw_limit",
            "raw_bytes",
        ),
        (
            event({"extra_content": {"signature": "opaque" * 100}}),
            {"stream_assembly_bytes": 100},
            "stream_assembly_limit",
            "assembly_bytes",
        ),
        (b'data: {"content":"sk-secret-local"\n\n', {}, "malformed_stream", None),
        (b"data: []\n\n", {}, "malformed_stream", None),
        (b'data: {"choices":[{"finish_reason":[]}]}\n\n', {}, "malformed_stream", None),
        (b'data: {"model":{},"choices":[]}\n\n', {}, "malformed_stream", None),
        (b'data: {"provider":[]}\n\n', {}, "malformed_stream", None),
        (b'data: {"choices":[{"delta":{"content":42}}]}\n\n', {}, "malformed_stream", None),
        (b'data: {"usage":{"total_tokens":NaN}}\n\n', {}, "malformed_stream", None),
        (b'data: {"choices":[{"delta":{"content":"\xff"}}]}\n\n', {}, "malformed_stream", None),
        (
            b'data: {"provider":"sk-secret-local","error":{"message":"private prompt secret sk-secret-local"}}\n\n',
            {},
            "provider_stream_error",
            None,
        ),
    ],
)
async def test_limits_and_malformed_data_have_bounded_diagnostics_without_replay(
    monkeypatch, payload, overrides, code, limit
):
    requests = 0
    diagnostics = []

    async def handler(request):
        nonlocal requests
        requests += 1
        return web.Response(body=payload, content_type="text/event-stream")

    async with server(monkeypatch, handler) as client:
        with pytest.raises(TurnError) as error:
            await conversation(
                client,
                stream_limits=replace(Limits(), **overrides),
                on_diagnostics=diagnostics.append,
            )
    failure = error.value
    assert failure.failure_code == code
    assert failure.diagnostics["failure_code"] == code
    assert failure.diagnostics["limit"] == limit
    assert diagnostics[-1]["failure_code"] == code
    assert failure.usage == {}
    assert requests == 1
    encoded = json.dumps(failure.diagnostics)
    assert len(encoded.encode()) < 4096
    assert "private prompt" not in encoded + str(failure)
    assert "sk-secret-local" not in encoded + str(failure)


async def test_utf8_split_and_multiline_sse_event(monkeypatch):
    async def handler(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        raw = (
            'data: {"choices": [\r\n'
            'data: {"delta":{"content":"café 🎉","reasoning":"想"},"finish_reason":"stop"}]}\r\n\r\n'
            "data: [DONE]\r\n\r\n"
        ).encode()
        for byte in raw:
            await response.write(bytes([byte]))
            await asyncio.sleep(0)
        return response

    async with server(monkeypatch, handler) as client:
        turn = await conversation(client)
    assert turn.message["content"] == "café 🎉"
    assert turn.adjustments["stream"]["bytes"]["content"] == len("café 🎉".encode())
    assert turn.adjustments["stream"]["bytes"]["reasoning"] == len("想".encode())


@pytest.mark.parametrize(
    "code,native,delta,usage,expected_code,retryable",
    [
        (503, None, {}, {}, 503, True),
        (503, None, {}, {"completion_tokens_details": {"reasoning_tokens": 0}}, 503, True),
        ("server_error", None, {}, {}, "server_error", True),
        (None, None, {}, {}, None, True),
        (400, None, {}, {}, 400, False),
        (503, "MALFORMED_FUNCTION_CALL", {}, {}, 503, False),
        (503, "private prompt", {}, {}, 503, False),
        ("private prompt sk-secret-local", None, {}, {}, None, False),
        ({"private": "sk-secret-local"}, None, {}, {}, None, False),
        (503, None, {"content": "partial"}, {}, 503, False),
        (503, None, {"tool_calls": [{"index": 0, "function": {"arguments": "{"}}]}, {}, 503, False),
        (503, None, {}, {"completion_tokens": 12}, 503, False),
        (503, None, {}, {"completion_tokens_details": {"reasoning_tokens": 12}}, 503, False),
    ],
)
async def test_provider_error_codes_are_safe_and_partial_errors_cannot_recover(
    monkeypatch, code, native, delta, usage, expected_code, retryable
):
    async def handler(request):
        payload = {
            "provider": "Google AI Studio",
            "error": {
                "code": code,
                "message": "private prompt sk-secret-local",
                "metadata": {"raw": "private prompt sk-secret-local"},
            },
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": "error",
                    "native_finish_reason": native,
                }
            ],
            "usage": usage,
        }
        return web.Response(
            text=f"data: {json.dumps(payload)}\n\n", content_type="text/event-stream"
        )

    async with server(monkeypatch, handler) as client:
        with pytest.raises(TurnError) as error:
            await conversation(client)
    diagnostics = error.value.diagnostics
    assert diagnostics["provider_error"] == {
        "code": expected_code,
        "native_finish_reason": native if native == "MALFORMED_FUNCTION_CALL" else None,
        "retryable_empty_response": retryable,
    }
    assert diagnostics["parsing"]["finish_reason"] == "error"
    assert diagnostics["provider"] == "Google AI Studio"
    assert error.value.usage == usage
    encoded = json.dumps(diagnostics)
    assert len(encoded.encode()) < 4096
    assert "private prompt" not in encoded and "sk-secret-local" not in encoded


@pytest.mark.parametrize("failure", [False, True])
async def test_prompt_echo_in_model_and_provider_metadata_is_omitted(monkeypatch, failure):
    private = "private prompt that must not reach diagnostics"
    diagnostics = []

    async def handler(request):
        metadata = {"model": private, "provider": private}
        if failure:
            metadata["error"] = {"message": private}
        else:
            metadata["choices"] = [{"delta": {"content": "ok"}, "finish_reason": "stop"}]
        body = ("data: " + json.dumps(metadata) + "\n\ndata: [DONE]\n\n").encode()
        return web.Response(body=body, content_type="text/event-stream")

    async with server(monkeypatch, handler) as client:
        if failure:
            with pytest.raises(TurnError) as error:
                await conversation(client, on_diagnostics=diagnostics.append)
            assert private not in str(error.value) + json.dumps(error.value.diagnostics)
        else:
            turn = await conversation(client, on_diagnostics=diagnostics.append)
            # Diagnostics filtering must not rewrite returned provider facts or
            # hide an unexpected model from the controller's validation.
            assert turn.model == turn.provider == private
            assert private not in json.dumps(turn.adjustments["stream"])
    assert diagnostics[-1]["requested_model"] == "test/model"
    assert diagnostics[-1]["model"] is None
    assert diagnostics[-1]["provider"] is None
    assert private not in json.dumps(diagnostics)


@pytest.mark.parametrize(
    "model,provider,expected_model,expected_provider",
    [
        ("test/model", "DeepSeek", "test/model", "DeepSeek"),
        ("model", "Google", "test/model", "Google"),
        ("catalog/model", "Google AI Studio", "catalog/model", "Google AI Studio"),
        ("private/prompt-secret", "Test Provider", None, None),
        ("model extra private words", "DeepSeek extra private words", None, None),
        ("test/sk-secret-local", "sk-secret-local", None, None),
    ],
)
def test_diagnostic_metadata_requires_recognized_public_identity(
    monkeypatch, model, provider, expected_model, expected_provider
):
    monkeypatch.setitem(api._MODEL_CONTEXT_CACHE, "catalog/model", 128_000)
    reader = StreamReader(StreamPolicy.resolve(Limits(), 1000), "test/model", "sk-secret-local")
    reader.assembly.model = model
    reader.assembly.provider = provider
    result = reader.diagnostics()
    assert result["model"] == expected_model
    assert result["provider"] == expected_provider


@pytest.mark.parametrize("action", ["cancel", "idle", "total"])
async def test_interruption_preserves_usage_and_partial_tool_diagnostics(monkeypatch, action):
    partial_received = asyncio.Event()
    release_server = asyncio.Event()
    diagnostics = []
    usage_updates = []
    requests = 0

    async def handler(request):
        nonlocal requests
        requests += 1
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(
            b'data: {"model":"test/model","provider":"DeepSeek","choices":[{"delta":{"tool_calls":[{"index":0,"id":"one","type":"function","function":{"name":"wb","arguments":"{\\"command\\":"}}]}}],"usage":{"prompt_tokens":10,"completion_tokens":3}}\n\n'
        )
        await release_server.wait()
        return response

    def on_usage(usage, output):
        usage_updates.append(usage)
        if usage:
            partial_received.set()

    limits = replace(
        Limits(),
        stream_seconds=1 if action == "total" else 3,
        stream_idle_seconds=1 if action == "idle" else 3,
    )
    async with server(monkeypatch, handler) as client:
        task = asyncio.create_task(
            conversation(
                client,
                stream_limits=limits,
                on_usage=on_usage,
                on_diagnostics=diagnostics.append,
            )
        )
        try:
            await asyncio.wait_for(partial_received.wait(), 2)
            if action == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(TurnError):
                    await asyncio.wait_for(task, 3)
        finally:
            release_server.set()
    assert requests == 1
    expected = {
        "cancel": "stream_cancelled",
        "idle": "stream_idle_timeout",
        "total": "stream_timeout",
    }
    assert diagnostics[-1]["failure_code"] == expected[action]
    assert diagnostics[-1]["parsing"]["tool_calls"] == 1
    assert diagnostics[-1]["parsing"]["done"] is False
    assert diagnostics[-1]["model"] == "test/model"
    assert diagnostics[-1]["provider"] == "DeepSeek"
    assert usage_updates[-1] == {"prompt_tokens": 10, "completion_tokens": 3}
