"""OpenRouter conversations with complete streamed tool calls and reasoning fields."""

from __future__ import annotations

import asyncio
import codecs
import copy
import json
from dataclasses import dataclass

import aiohttp

from wavebench import api
from wavebench.prompt_cache import CachePolicy, affinity
from wavebench.tokens import count_tokens


@dataclass
class Turn:
    message: dict
    usage: dict
    model: str | None
    provider: str | None
    finish_reason: str
    adjustments: dict


class TurnError(RuntimeError):
    def __init__(self, message: str, usage: dict | None = None, *, request_sent: bool = True):
        super().__init__(message)
        self.usage = usage or {}
        self.request_sent = request_sent


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

    def output_text(self) -> str:
        """Count generated text once, excluding SSE framing and opaque signatures."""
        reasoning = self.message.get("reasoning") or "".join(
            detail.get("text") or detail.get("summary") or ""
            for detail in self.details.values()
            if detail.get("type") in {"reasoning.text", "reasoning.summary"}
        )
        parts = [self.message.get("content") or "", reasoning]
        for call in self.calls.values():
            function = call.get("function") or {}
            parts.extend([function.get("name") or "", function.get("arguments") or ""])
        return "\n".join(part for part in parts if part)

    @property
    def chars(self) -> int:
        return len(self.output_text())

    @staticmethod
    def merge_usage(target: dict, update: dict) -> None:
        """Usage chunks are cumulative snapshots, never additive deltas."""
        for key, value in update.items():
            if value is None:
                continue
            if isinstance(value, dict):
                StreamAssembly.merge_usage(target.setdefault(key, {}), value)
            else:
                target[key] = value

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
        if obj.get("usage"):
            self.merge_usage(self.usage, obj["usage"])
        if obj.get("error"):
            raise TurnError(f"mid-stream error: {str(obj['error'])[:500]}", self.usage)
        self.model = obj.get("model") or self.model
        self.provider = obj.get("provider") or self.provider
        for choice in obj.get("choices") or []:
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
    input_tokens_bound: int | None = None,
    cache_policy: CachePolicy | None = None,
    cache_reuse: bool = True,
    strict_reasoning: bool = False,
    on_progress=None,
    on_usage=None,
    on_retry=None,
) -> Turn:
    """Retry rejected HTTP requests only; never replay a partially received turn.

    ``on_usage`` receives raw provider usage and a separate local output-token
    estimate. The estimate tracks visible streaming progress, not billable usage.
    """
    await api._load_model_context_lengths(session, api_key)
    serialized = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)
    # The controller can anchor its estimate to provider-reported prompt usage.
    # Standalone callers fall back to bytes for the entire request.
    context_bound = (
        len(serialized.encode("utf-8")) if input_tokens_bound is None else input_tokens_bound
    ) + 1024
    context_limit = api._MODEL_CONTEXT_CACHE.get(model_id, 128_000)
    resolved = min(
        max_tokens,
        context_limit - context_bound,
        api._MODEL_MAX_COMPLETION_CACHE.get(model_id, max_tokens),
    )
    if resolved < 1:
        raise TurnError(
            "conversation context budget exhausted; no request sent", request_sent=False
        )
    reasoning = (
        api._reasoning_attempts(model_id, reasoning_effort, resolved) if reasoning_effort else []
    ) or [{}]
    if strict_reasoning:
        reasoning = [{"reasoning": {"effort": reasoning_effort}}]
    if cache_reuse:
        policy = cache_policy or CachePolicy(model_id, json.dumps(messages[:2], ensure_ascii=False))
        cache_payload, cache_record = policy.prepare(messages, tools)
    else:
        cache_payload = {"messages": messages, **affinity(model_id, messages[0]["content"])}
        if model_id == "openai/gpt-5.6-luna":
            # A compactor's input is used once. Explicit mode with no breakpoints
            # avoids a paid cache write that no later request will read.
            cache_payload["prompt_cache_options"] = {"mode": "explicit", "ttl": "30m"}
        cache_record = {"policy": "single_use", "breakpoints": []}
    reasoning_index = 0
    adjustments = {
        "requested_max_tokens": max_tokens,
        "max_tokens": resolved,
        "context_limit": context_limit,
        "context_bound": context_bound,
        "cache": cache_record,
    }
    for request_index in range(api._MAX_RETRIES + 1):
        data = {
            "model": model_id,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": resolved,
            "provider": {"require_parameters": True},
            **reasoning[reasoning_index],
            **cache_payload,
        }
        if tools:
            data["tools"] = tools
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
                output_tokens = 0
                last_text = ""
                last_usage = {}

                async def report_usage(force=False, assembly=assembly):
                    nonlocal output_tokens, last_text, last_usage
                    if not on_usage:
                        return
                    text = assembly.output_text()
                    if force or text != last_text or assembly.usage != last_usage:
                        # Publish every received text batch, including the last batch
                        # before a pause. Heartbeats/usage-only frames need no recount.
                        # Keep visible output separate from provider accounting so a
                        # final usage correction cannot look like a burst of tokens.
                        if text != last_text:
                            output_tokens = await asyncio.to_thread(count_tokens, text)
                        on_usage(copy.deepcopy(assembly.usage), output_tokens)
                        last_text = text
                        last_usage = copy.deepcopy(assembly.usage)

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
                        await report_usage()
                    buffer += decoder.decode(b"", final=True)
                    if buffer.startswith("data:"):
                        assembly.feed(buffer[5:].strip())
                    await report_usage(force=True)
                    turn = assembly.complete()
                except (aiohttp.ClientError, UnicodeError) as exc:
                    raise TurnError(f"incomplete stream: {exc}", assembly.usage) from exc
                finally:
                    # Preserve any reported billable usage even on timeout/cancellation.
                    if on_usage:
                        on_usage(copy.deepcopy(assembly.usage), output_tokens)
                turn.adjustments = {**adjustments, "reasoning": reasoning[reasoning_index]}
                return turn
        await asyncio.sleep(wait)
    raise TurnError("HTTP retry budget exhausted")
