"""OpenRouter conversations with complete streamed tool calls and reasoning fields."""

from __future__ import annotations

import asyncio
import codecs
import copy
import json
from dataclasses import dataclass

import aiohttp

from wavebench import api


@dataclass
class Turn:
    message: dict
    usage: dict
    model: str | None
    provider: str | None
    finish_reason: str
    adjustments: dict


class TurnError(RuntimeError):
    def __init__(self, message: str, usage: dict | None = None):
        super().__init__(message)
        self.usage = usage or {}


class StreamAssembly:
    """No tools can be dispatched until finish, full JSON validation, and EOF agree."""

    def __init__(self):
        self.message = {"role": "assistant", "content": ""}
        self.calls: dict[int, dict] = {}
        self.details: dict[int, dict] = {}
        self.usage: dict = {}
        self.model = None
        self.provider = None
        self.finish = ""
        self.done = False
        self.chars = 0

    @staticmethod
    def merge(target: dict, delta: dict) -> None:
        for key, value in delta.items():
            if value is None:
                continue
            if isinstance(value, dict):
                StreamAssembly.merge(target.setdefault(key, {}), value)
            elif isinstance(value, str) and key not in {"id", "type", "format", "role"}:
                target[key] = target.get(key, "") + value
            else:
                target[key] = copy.deepcopy(value)

    def feed(self, payload: str) -> None:
        if payload == "[DONE]":
            self.done = True
            return
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise TurnError("malformed SSE JSON; no tools executed", self.usage) from exc
        if obj.get("error"):
            raise TurnError(f"mid-stream error: {str(obj['error'])[:500]}", self.usage)
        if obj.get("usage"):
            self.usage = obj["usage"]
        self.model = obj.get("model") or self.model
        self.provider = obj.get("provider") or self.provider
        for choice in obj.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            self.finish = choice.get("finish_reason") or self.finish
            delta = dict(choice.get("delta") or choice.get("message") or {})
            for call in delta.pop("tool_calls", None) or []:
                index = call.get("index", 0)
                if type(index) is not int or index < 0 or index >= 256:
                    raise TurnError("invalid streamed tool index", self.usage)
                self.merge(
                    self.calls.setdefault(index, {}),
                    {k: v for k, v in call.items() if k != "index"},
                )
            for detail in delta.pop("reasoning_details", None) or []:
                index = detail.get("index", len(self.details))
                self.merge(self.details.setdefault(index, {}), detail)
            self.merge(self.message, delta)
            self.chars += len(json.dumps(delta)) + sum(
                len(c.get("function", {}).get("arguments", ""))
                for c in choice.get("delta", {}).get("tool_calls", []) or []
            )

    def complete(self) -> Turn:
        if not self.done or self.finish not in {"stop", "tool_calls"}:
            raise TurnError(
                f"incomplete response ({self.finish or 'EOF'}); no tools executed", self.usage
            )
        if self.calls:
            calls = [self.calls[index] for index in sorted(self.calls)]
            seen = set()
            for call in calls:
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id or call_id in seen:
                    raise TurnError(
                        "missing or duplicate tool call ID; no tools executed", self.usage
                    )
                seen.add(call_id)
                if call.get("type") != "function" or not isinstance(call.get("function"), dict):
                    raise TurnError("invalid tool call; no tools executed", self.usage)
                try:
                    arguments = json.loads(call["function"]["arguments"])
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise TurnError(
                        "malformed/truncated tool arguments; no tools executed", self.usage
                    ) from exc
                if not isinstance(arguments, dict):
                    raise TurnError(
                        "tool arguments must be an object; no tools executed", self.usage
                    )
            self.message["tool_calls"] = calls
        if self.details:
            self.message["reasoning_details"] = [
                self.details[index] for index in sorted(self.details)
            ]
        return Turn(self.message, self.usage, self.model, self.provider, self.finish, {})


async def capability(session, api_key, model_id) -> bool | None:
    await api._load_model_context_lengths(session, api_key)
    return api._MODEL_TOOL_CACHE.get(model_id)


async def call_conversation(
    session,
    api_key: str,
    model_id: str,
    messages: list[dict],
    tools: list[dict],
    *,
    max_tokens: int,
    reasoning_effort: str | None,
    on_progress=None,
    on_retry=None,
) -> Turn:
    """Retry rejected HTTP requests only; never replay a partially received turn."""
    await api._load_model_context_lengths(session, api_key)
    serialized = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)
    # UTF-8 bytes conservatively bound tokenization, including tool results/schema.
    context_bound = len(serialized.encode("utf-8")) + 1024
    context_limit = api._MODEL_CONTEXT_CACHE.get(model_id, 128_000)
    resolved = min(
        max_tokens,
        context_limit - context_bound,
        api._MODEL_MAX_COMPLETION_CACHE.get(model_id, max_tokens),
    )
    if resolved < 1:
        raise TurnError("conversation context budget exhausted; no request sent")
    reasoning = (
        api._reasoning_attempts(model_id, reasoning_effort, resolved) if reasoning_effort else []
    ) or [{}]
    reasoning_index = 0
    adjustments = {
        "requested_max_tokens": max_tokens,
        "max_tokens": resolved,
        "context_limit": context_limit,
        "context_bound": context_bound,
    }
    for request_index in range(api._MAX_RETRIES + 1):
        data = {
            "model": model_id,
            "messages": messages,
            "tools": tools,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": resolved,
            "provider": {"require_parameters": True},
            **reasoning[reasoning_index],
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "WaveBench Harness",
        }
        async with session.post(
            f"{api.API_URL}/chat/completions", headers=headers, json=data
        ) as response:
            if response.status != 200:
                error = (await response.text())[:2000]
                retryable = response.status in api._RETRYABLE_STATUSES
                if (
                    response.status == 400
                    and "reasoning" in error.lower()
                    and reasoning_index + 1 < len(reasoning)
                ):
                    reasoning_index += 1
                    retryable = True
                token_limit = (
                    api._credit_token_limit_from_error(error) if response.status == 402 else None
                )
                if token_limit and token_limit < resolved:
                    resolved = token_limit
                    retryable = True
                if not retryable or request_index == api._MAX_RETRIES:
                    label = (
                        "unsupported tool calling"
                        if "tool" in error.lower() and response.status in {400, 404, 422}
                        else f"HTTP {response.status}"
                    )
                    raise TurnError(f"{label}: {error[:500]}")
                wait = (
                    api._retry_wait_seconds(response.headers.get("Retry-After"), request_index + 1)
                    if response.status in api._RETRYABLE_STATUSES
                    else 0
                )
                adjustments.update(max_tokens=resolved, reasoning=reasoning[reasoning_index])
                if on_retry:
                    on_retry(response.status, request_index + 1, api._MAX_RETRIES, wait)
            else:
                assembly = StreamAssembly()
                decoder = codecs.getincrementaldecoder("utf-8")("strict")
                buffer = ""
                received = 0
                try:
                    async for raw in response.content.iter_any():
                        received += len(raw)
                        if received > 8 * 1024 * 1024:
                            raise TurnError("stream byte budget exhausted", assembly.usage)
                        buffer += decoder.decode(raw)
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            if line.startswith("data:"):
                                assembly.feed(line[5:].strip())
                        if on_progress:
                            on_progress(assembly.chars)
                    buffer += decoder.decode(b"", final=True)
                    if buffer.startswith("data:"):
                        assembly.feed(buffer[5:].strip())
                    turn = assembly.complete()
                except (aiohttp.ClientError, UnicodeError) as exc:
                    raise TurnError(f"incomplete stream: {exc}", assembly.usage) from exc
                turn.adjustments = {**adjustments, "reasoning": reasoning[reasoning_index]}
                return turn
        await asyncio.sleep(wait)
    raise TurnError("HTTP retry budget exhausted")
