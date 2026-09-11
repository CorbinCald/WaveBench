"""OpenRouter conversations with complete streamed tool calls and reasoning fields."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from dataclasses import asdict, dataclass

import aiohttp

from wavebench import api
from wavebench.prompt_cache import CachePolicy, affinity
from wavebench.tokens import count_tokens

from .config import Limits

# Display names and routing slugs from OpenRouter's provider catalogue, 2026-09-11.
GEMINI_PROVIDER_ROUTES = {"Google": "google-vertex", "Google AI Studio": "google-ai-studio"}


def thought_signature_error(error) -> bool:
    """Recognize the provider's error without retaining its private response body."""
    if not isinstance(error, dict):
        return False
    messages = [error.get("message")]
    metadata = error.get("metadata")
    raw = metadata.get("raw") if isinstance(metadata, dict) else None
    if isinstance(raw, str) and len(raw) <= 8192:
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            messages.append(raw)
    if isinstance(raw, dict):
        nested = raw.get("error", raw)
        if isinstance(nested, dict):
            messages.append(nested.get("message"))
    return any(
        isinstance(message, str)
        and re.search(r"thought[ _-]?signature", message, re.IGNORECASE)
        and re.search(r"\b(corrupt(?:ed)?|invalid|missing)\b", message, re.IGNORECASE)
        for message in messages
    )


# Public display names from https://openrouter.ai/api/v1/providers, 2026-09-11.
# Unknown labels are omitted from diagnostics until this snapshot is refreshed.
# Do not fetch a provider catalogue during model requests or trust streamed prose.
PUBLIC_PROVIDER_NAMES = frozenset(
    {
        "AI21",
        "AionLabs",
        "AkashML",
        "Alibaba",
        "Amazon Bedrock",
        "Amazon Nova",
        "Ambient",
        "Anthropic",
        "Arcee AI",
        "AtlasCloud",
        "Avian",
        "Azure",
        "Baidu",
        "BaseTen",
        "Black Forest Labs",
        "Cerebras",
        "Chutes",
        "Cirrascale",
        "Clarifai",
        "Claude Platform on AWS",
        "Cloudflare",
        "Cohere",
        "CoreWeave",
        "Cosine",
        "Crucible",
        "Crusoe",
        "Darkbloom",
        "Databricks",
        "Decart",
        "DeepInfra",
        "DeepSeek",
        "Deepgram",
        "DekaLLM",
        "DigitalOcean",
        "FakeProvider",
        "Featherless",
        "Fireworks",
        "Fish Audio",
        "Friendli",
        "GMICloud",
        "Google",
        "Google AI Studio",
        "Groq",
        "HeyGen",
        "Inception",
        "Inceptron",
        "Inferact vLLM",
        "InferenceNet",
        "Infermatic",
        "Inflection",
        "Io Net",
        "Ionstream",
        "Krea",
        "Liquid",
        "Makora",
        "Mancer 2",
        "Mara",
        "Meta",
        "Minimax",
        "Mistral",
        "Modal",
        "ModelRun",
        "Modular",
        "Moonshot AI",
        "Morph",
        "Near AI",
        "Nebius",
        "Nex AGI",
        "NextBit",
        "Novita",
        "Nvidia",
        "Ollama",
        "OpenAI",
        "OpenInference",
        "Parasail",
        "Perceptron",
        "Perplexity",
        "Phala",
        "Poolside",
        "PrimeIntellect",
        "Quiver",
        "Recraft",
        "Reka",
        "Relace",
        "Runway",
        "Sail Research",
        "Sakana AI",
        "SambaNova",
        "Seed",
        "SiliconFlow",
        "Sourceful",
        "Stealth",
        "StepFun",
        "StreamLake",
        "Switchpoint",
        "Tencent",
        "Tenstorrent",
        "Thinking Machines",
        "Together",
        "Upstage",
        "Venice",
        "VoyageAI by MongoDB",
        "Wafer",
        "Xiaomi",
        "Z.AI",
        "xAI",
    }
)


@dataclass
class Turn:
    message: dict
    usage: dict
    model: str | None
    provider: str | None
    finish_reason: str
    adjustments: dict


class TurnError(RuntimeError):
    def __init__(
        self,
        message: str,
        usage: dict | None = None,
        *,
        request_sent: bool = True,
        failure_code: str | None = None,
        diagnostics: dict | None = None,
    ):
        super().__init__(message)
        self.usage = usage or {}
        self.request_sent = request_sent
        self.failure_code = failure_code
        self.diagnostics = diagnostics or {}


@dataclass(frozen=True)
class StreamPolicy:
    """Effective byte guards for one resolved output allowance, not token usage."""

    output_tokens: int
    raw_bytes: int
    output_bytes: int
    frame_bytes: int
    assembly_bytes: int
    seconds: int
    idle_seconds: int

    @classmethod
    def resolve(cls, limits: Limits, max_tokens: int) -> StreamPolicy:
        return cls(
            output_tokens=max_tokens,
            raw_bytes=min(
                limits.stream_raw_max_bytes,
                max(limits.stream_raw_min_bytes, max_tokens * limits.stream_raw_bytes_per_token),
            ),
            output_bytes=min(
                limits.stream_output_max_bytes,
                max(
                    limits.stream_output_min_bytes,
                    max_tokens * limits.stream_output_bytes_per_token,
                ),
            ),
            frame_bytes=limits.stream_frame_bytes,
            assembly_bytes=limits.stream_assembly_bytes,
            seconds=limits.stream_seconds,
            idle_seconds=limits.stream_idle_seconds,
        )

    def record(self) -> dict:
        return asdict(self)


class StreamAssembly:
    """No tools can be dispatched until finish, full JSON validation, and EOF agree."""

    def __init__(self, policy: StreamPolicy | None = None):
        self.policy = policy or StreamPolicy.resolve(Limits(), Limits().turn_tokens)
        self.message = {"role": "assistant", "content": ""}
        self.calls: dict[int, dict] = {}
        self.details: dict[int, dict] = {}
        self.usage: dict = {}
        self.model = None
        self.provider = None
        self.provider_error: dict = {}
        self.finish = ""
        self.done = False
        self.content_bytes = 0
        self.reasoning_bytes = 0
        self.tool_arguments_bytes = 0
        self.tool_name_bytes = 0
        self.assembly_bytes = 0
        self.events = 0

    @property
    def output_bytes(self) -> int:
        return (
            self.content_bytes
            + self.reasoning_bytes
            + self.tool_arguments_bytes
            + self.tool_name_bytes
        )

    def retain(self, value) -> None:
        # Charging each retained delta before merging is a conservative bound:
        # replacements can overcount, but unknown provider fields cannot evade it.
        self.assembly_bytes += len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if self.assembly_bytes > self.policy.assembly_bytes:
            raise TurnError(
                "stream assembly byte limit exceeded; no tools executed",
                self.usage,
                failure_code="stream_assembly_limit",
            )

    def count_output(self, delta: dict) -> None:
        for key, counter in (("content", "content_bytes"), ("reasoning", "reasoning_bytes")):
            value = delta.get(key)
            if value is not None and not isinstance(value, str):
                raise TurnError(
                    "malformed streamed text", self.usage, failure_code="malformed_stream"
                )
            setattr(self, counter, getattr(self, counter) + len((value or "").encode("utf-8")))
        for detail in delta.get("reasoning_details") or []:
            for key in ("text", "summary"):
                value = detail.get(key)
                if value is not None and not isinstance(value, str):
                    raise TurnError(
                        "malformed streamed reasoning", self.usage, failure_code="malformed_stream"
                    )
                self.reasoning_bytes += len((value or "").encode("utf-8"))
        for call in delta.get("tool_calls") or []:
            function = call.get("function") or {}
            for key, counter in (
                ("arguments", "tool_arguments_bytes"),
                ("name", "tool_name_bytes"),
            ):
                value = function.get(key)
                if value is not None and not isinstance(value, str):
                    raise TurnError(
                        "malformed streamed tool text", self.usage, failure_code="malformed_stream"
                    )
                setattr(self, counter, getattr(self, counter) + len((value or "").encode("utf-8")))
        if self.output_bytes > self.policy.output_bytes:
            raise TurnError(
                "stream generated-output byte limit exceeded; no tools executed",
                self.usage,
                failure_code="stream_output_limit",
            )

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
        try:
            self._feed(payload)
        except (AttributeError, TypeError, ValueError, RecursionError) as exc:
            raise TurnError(
                "malformed stream data; no tools executed",
                self.usage,
                failure_code="malformed_stream",
            ) from exc

    def _feed(self, payload: str) -> None:
        self.events += 1
        if self.done:
            raise TurnError(
                "data received after stream completion", self.usage, failure_code="malformed_stream"
            )
        if payload == "[DONE]":
            self.done = True
            return
        try:
            obj = json.loads(payload, parse_constant=self.invalid_constant)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise TurnError(
                "malformed SSE JSON; no tools executed", self.usage, failure_code="malformed_stream"
            ) from exc
        if not isinstance(obj, dict):
            raise TurnError(
                "malformed SSE object; no tools executed",
                self.usage,
                failure_code="malformed_stream",
            )
        if obj.get("usage"):
            if not isinstance(obj["usage"], dict):
                raise TurnError(
                    "malformed stream usage", self.usage, failure_code="malformed_stream"
                )
            self.retain(obj["usage"])
            self.merge_usage(self.usage, obj["usage"])
        for key in ("model", "provider"):
            value = obj.get(key)
            if value is not None:
                if not isinstance(value, str) or len(value.encode("utf-8")) > 512:
                    raise TurnError(
                        "malformed stream metadata", self.usage, failure_code="malformed_stream"
                    )
                setattr(self, key, value or getattr(self, key))
        if obj.get("error"):
            # Provider messages can echo request content or credentials. Keep
            # only recognized codes and whether output preceded the failure.
            self.record_provider_error(obj)
            invalid_signature = thought_signature_error(obj["error"])
            if invalid_signature:
                self.provider_error["retryable_empty_response"] = False
            raise TurnError(
                "provider rejected Gemini thought signature; no tools executed"
                if invalid_signature
                else "mid-stream provider error; no tools executed",
                self.usage,
                failure_code="thought_signature_invalid"
                if invalid_signature
                else "provider_stream_error",
            )
        choices = obj.get("choices")
        if choices is not None and not isinstance(choices, list):
            raise TurnError("malformed stream choices", self.usage, failure_code="malformed_stream")
        for choice in choices or []:
            if choice.get("index", 0) != 0:
                continue
            finish = choice.get("finish_reason")
            if finish is not None and not isinstance(finish, str):
                raise TurnError(
                    "malformed stream finish reason", self.usage, failure_code="malformed_stream"
                )
            self.finish = finish or self.finish
            source = choice.get("delta") or choice.get("message") or {}
            if not isinstance(source, dict):
                raise TurnError(
                    "malformed stream delta", self.usage, failure_code="malformed_stream"
                )
            delta = dict(source)
            self.count_output(delta)
            self.retain(delta)
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
                if type(index) is not int or index < 0 or index >= 256:
                    raise TurnError(
                        "invalid streamed reasoning index",
                        self.usage,
                        failure_code="malformed_stream",
                    )
                self.merge(self.details.setdefault(index, {}), detail)
            self.merge(self.message, delta)

    def record_provider_error(self, obj: dict) -> None:
        error = obj["error"]
        code = error.get("code") if isinstance(error, dict) else None
        known_codes = {
            "server_error",
            "rate_limit_exceeded",
            "invalid_api_key",
            "invalid_request_error",
            "insufficient_quota",
            "context_length_exceeded",
        }
        safe_code = (
            code
            if (type(code) is int and 400 <= code <= 599)
            or (isinstance(code, str) and code in known_codes)
            else None
        )
        has_output = any(value for key, value in self.message.items() if key != "role")
        has_output = bool(has_output or self.calls or self.details or self.output_bytes)
        completion_details = self.usage.get("completion_tokens_details") or {}
        has_output |= (
            any(completion_details.values()) if isinstance(completion_details, dict) else True
        )
        native_finish = None
        has_native_finish = False
        choices = obj.get("choices")
        if choices is not None and not isinstance(choices, list):
            has_output = True
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, dict):
                has_output = True
                continue
            if choice.get("index", 0) != 0:
                continue
            source = choice.get("delta") or choice.get("message") or {}
            if isinstance(source, dict):
                has_output |= any(value for key, value in source.items() if key != "role")
            else:
                has_output = True
            finish = choice.get("native_finish_reason")
            has_native_finish |= bool(finish)
            if isinstance(finish, str) and finish in {
                "MALFORMED_FUNCTION_CALL",
                "UNEXPECTED_TOOL_CALL",
                "TOO_MANY_TOOL_CALLS",
                "MAX_TOKENS",
                "SAFETY",
                "RECITATION",
                "BLOCKLIST",
                "PROHIBITED_CONTENT",
                "SPII",
                "OTHER",
                "ERROR",
            }:
                native_finish = finish
            if choice.get("finish_reason") == "error":
                self.finish = "error"
        self.provider_error = {
            "code": safe_code,
            "native_finish_reason": native_finish,
            "retryable_empty_response": (
                not has_output
                and not self.usage.get("completion_tokens")
                and not has_native_finish
                and (
                    code is None
                    or safe_code
                    in {
                        408,
                        429,
                        500,
                        502,
                        503,
                        504,
                        "server_error",
                        "rate_limit_exceeded",
                    }
                )
            ),
        }

    @staticmethod
    def invalid_constant(value):
        raise ValueError("non-finite JSON number")

    def complete(self) -> Turn:
        if not self.done or self.finish not in {"stop", "tool_calls"}:
            reason = self.finish if self.finish in {"length", "error", "content_filter"} else "EOF"
            raise TurnError(
                f"incomplete response ({reason}); no tools executed",
                self.usage,
                failure_code="output_truncated" if self.finish == "length" else "incomplete_stream",
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
                if (
                    not isinstance(call["function"].get("name"), str)
                    or not call["function"]["name"]
                ):
                    raise TurnError("missing tool function name; no tools executed", self.usage)
                try:
                    arguments = json.loads(
                        call["function"]["arguments"], parse_constant=self.invalid_constant
                    )
                except (KeyError, TypeError, ValueError, RecursionError) as exc:
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


class StreamReader:
    """Bound wire data, incomplete SSE events, and retained parsed response separately."""

    def __init__(self, policy: StreamPolicy, model_id: str, api_key: str, *, pinned_provider=None):
        self.policy = policy
        self.assembly = StreamAssembly(policy)
        self.model_id = model_id
        self.api_key = api_key
        self.pinned_provider = pinned_provider
        self.raw_bytes = 0
        self.buffer = b""
        self.data: list[str] = []
        self.frame_bytes = 0
        self.started = time.monotonic()
        self.failure_code: str | None = None

    def limit(self, code: str, label: str) -> None:
        raise TurnError(
            f"stream {label} limit exceeded; no tools executed",
            self.assembly.usage,
            failure_code=code,
        )

    def line(self, raw: bytes) -> None:
        self.frame_bytes += len(raw)
        if self.frame_bytes > self.policy.frame_bytes:
            self.limit("stream_frame_limit", "incomplete-frame byte")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            self.dispatch()
        elif line.startswith("data:"):
            value = line[5:]
            self.data.append(value[1:] if value.startswith(" ") else value)
        elif not self.data:
            # Comments and ignored SSE fields need no retention. A stream of
            # valid heartbeats consumes only the raw-byte/time allowances.
            self.frame_bytes = 0

    def dispatch(self) -> None:
        if self.data:
            self.assembly.feed("\n".join(self.data))
        self.data.clear()
        self.frame_bytes = 0

    def feed(self, raw: bytes) -> None:
        self.raw_bytes += len(raw)
        if self.raw_bytes > self.policy.raw_bytes:
            self.limit("stream_raw_limit", "raw-wire byte")
        self.buffer += raw
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self.line(line + b"\n")
        if self.frame_bytes + len(self.buffer) > self.policy.frame_bytes:
            self.limit("stream_frame_limit", "incomplete-frame byte")

    def complete(self) -> Turn:
        if self.buffer:
            self.line(self.buffer)
            self.buffer = b""
        self.dispatch()
        return self.assembly.complete()

    def identifier(self, value) -> str | None:
        # Character and credential checks supplement the identity allowlists.
        # A short prose string alone is not a safe model or provider identifier.
        if not isinstance(value, str) or not re.fullmatch(r"[\w ./():-]{1,128}", value):
            return None
        if self.api_key and self.api_key in value:
            return None
        if re.search(r"(?i)(bearer|api[ _-]?key|sk-)", value):
            return None
        return value

    def model_identifier(self, value) -> str | None:
        value = self.identifier(value)
        pattern = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}"
        if value is None or not re.fullmatch(pattern, value):
            return None
        if value in {self.model_id, self.model_id.rsplit("/", 1)[-1]}:
            canonical = self.identifier(self.model_id)
            return canonical if canonical and re.fullmatch(pattern, canonical) else None
        # The existing model catalogue is loaded before streaming begins. Use
        # only its exact public IDs, never a label supplied by the response.
        return value if value in api._MODEL_CONTEXT_CACHE else None

    def provider_identifier(self, value) -> str | None:
        value = self.identifier(value)
        return value if value in PUBLIC_PROVIDER_NAMES else None

    def diagnostics(self) -> dict:
        assembly = self.assembly
        return {
            "policy": self.policy.record(),
            "failure_code": self.failure_code,
            "limit": {
                "stream_raw_limit": "raw_bytes",
                "stream_output_limit": "output_bytes",
                "stream_frame_limit": "frame_bytes",
                "stream_assembly_limit": "assembly_bytes",
                "stream_timeout": "seconds",
                "stream_idle_timeout": "idle_seconds",
            }.get(self.failure_code),
            "bytes": {
                "raw": self.raw_bytes,
                "content": assembly.content_bytes,
                "reasoning": assembly.reasoning_bytes,
                "tool_arguments": assembly.tool_arguments_bytes,
                "tool_names": assembly.tool_name_bytes,
                "output": assembly.output_bytes,
                "assembly": assembly.assembly_bytes,
            },
            "parsing": {
                "events": assembly.events,
                "pending_line_bytes": len(self.buffer),
                "pending_frame_bytes": self.frame_bytes + len(self.buffer),
                "tool_calls": len(assembly.calls),
                "reasoning_details": len(assembly.details),
                "finish_reason": assembly.finish
                if assembly.finish in {"stop", "tool_calls", "length", "error", "content_filter"}
                else None,
                "done": assembly.done,
            },
            "requested_model": self.model_identifier(self.model_id),
            "model": self.model_identifier(assembly.model),
            "provider": self.provider_identifier(assembly.provider),
            "pinned_provider": self.provider_identifier(self.pinned_provider),
            "provider_error": assembly.provider_error.copy(),
            "provider_usage_available": bool(assembly.usage),
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
        }


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
    gemini_provider: str | None = None,
    cache_reuse: bool = True,
    strict_reasoning: bool = False,
    stream_limits: Limits | None = None,
    on_progress=None,
    on_usage=None,
    on_retry=None,
    on_diagnostics=None,
) -> Turn:
    """Retry rejected HTTP requests only; never replay a partially received turn.

    ``on_usage`` receives raw provider usage and a separate local output-token
    estimate. The estimate tracks visible streaming progress, not billable usage.
    """
    provider_routing = {"require_parameters": True}
    if gemini_provider is not None:
        if gemini_provider not in GEMINI_PROVIDER_ROUTES:
            raise TurnError(
                "Gemini provider identity unavailable; no request sent",
                request_sent=False,
                failure_code="provider_identity_missing",
            )
        provider_routing.update(
            only=[GEMINI_PROVIDER_ROUTES[gemini_provider]], allow_fallbacks=False
        )
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
        "provider_routing": provider_routing,
    }
    for request_index in range(api._MAX_RETRIES + 1):
        data = {
            "model": model_id,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": resolved,
            "provider": provider_routing,
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
                # Rejected responses are untrusted too. Read a bounded prefix
                # for retry negotiation, and never copy provider bodies into
                # shared errors: they can echo credentials or private prompts.
                error = (
                    await asyncio.wait_for(
                        response.content.read(4096),
                        (stream_limits or Limits()).stream_idle_seconds,
                    )
                ).decode("utf-8", errors="replace")
                try:
                    error_body = json.loads(error)
                except (ValueError, RecursionError):
                    error_body = {}
                if isinstance(error_body, dict) and thought_signature_error(
                    error_body.get("error")
                ):
                    raise TurnError(
                        "provider rejected Gemini thought signature; no tools executed",
                        failure_code="thought_signature_invalid",
                        diagnostics={
                            "http_status": response.status,
                            "pinned_provider": gemini_provider,
                        },
                    )
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
                    code, label = "http_error", f"HTTP {response.status}"
                    if "tool" in error.lower() and response.status in {400, 404, 422}:
                        code, label = "unsupported_tools", "unsupported tool calling"
                    elif "reasoning" in error.lower() and response.status == 400:
                        code, label = "reasoning_rejected", "reasoning configuration rejected"
                    raise TurnError(
                        f"{label}; provider rejected the request",
                        failure_code=code,
                        diagnostics={"http_status": response.status},
                    )
                wait = (
                    api._retry_wait_seconds(response.headers.get("Retry-After"), request_index + 1)
                    if response.status in api._RETRYABLE_STATUSES
                    else 0
                )
                adjustments.update(max_tokens=resolved, reasoning=reasoning[reasoning_index])
                if on_retry:
                    on_retry(response.status, request_index + 1, api._MAX_RETRIES, wait)
            else:
                policy = StreamPolicy.resolve(stream_limits or Limits(), resolved)
                reader = StreamReader(policy, model_id, api_key, pinned_provider=gemini_provider)
                assembly = reader.assembly
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
                    # Small reads keep transient memory bounded even when one
                    # response event contains a large tool-argument fragment.
                    chunks = response.content.iter_chunked(64 * 1024).__aiter__()
                    while True:
                        remaining = policy.seconds - (time.monotonic() - reader.started)
                        if remaining <= 0:
                            reader.limit("stream_timeout", "total time")
                        try:
                            raw = await asyncio.wait_for(
                                chunks.__anext__(), min(remaining, policy.idle_seconds)
                            )
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError as exc:
                            code = (
                                "stream_timeout"
                                if remaining <= policy.idle_seconds
                                else "stream_idle_timeout"
                            )
                            raise TurnError(
                                "stream total time limit exceeded"
                                if code == "stream_timeout"
                                else "stream idle time limit exceeded",
                                assembly.usage,
                                failure_code=code,
                            ) from exc
                        reader.feed(raw)
                        if on_progress:
                            on_progress(assembly.chars)
                        await report_usage()
                    turn = reader.complete()
                    await report_usage(force=True)
                    if gemini_provider is not None and turn.provider != gemini_provider:
                        raise TurnError(
                            "Gemini provider changed despite routing restriction; no tools executed",
                            turn.usage,
                            failure_code="provider_changed",
                        )
                except asyncio.CancelledError:
                    reader.failure_code = "stream_cancelled"
                    raise
                except TurnError as exc:
                    reader.failure_code = exc.failure_code or "malformed_stream"
                    exc.failure_code = reader.failure_code
                    exc.diagnostics = reader.diagnostics()
                    raise
                except (aiohttp.ClientError, UnicodeError) as exc:
                    reader.failure_code = (
                        "malformed_stream"
                        if isinstance(exc, UnicodeError)
                        else "stream_disconnected"
                    )
                    raise TurnError(
                        "malformed UTF-8 stream; no tools executed"
                        if isinstance(exc, UnicodeError)
                        else "incomplete stream: connection interrupted; no tools executed",
                        assembly.usage,
                        failure_code=reader.failure_code,
                        diagnostics=reader.diagnostics(),
                    ) from exc
                finally:
                    # One network read can contain valid output followed by a
                    # malformed event. Reconcile that retained output even when
                    # feed() or its tokenization was interrupted, without
                    # inventing provider usage. Cancellation waits for this
                    # bounded accounting cleanup before leaving the request.
                    try:
                        if on_usage:
                            final_usage = asyncio.create_task(report_usage(force=True))
                            try:
                                await asyncio.shield(final_usage)
                            except asyncio.CancelledError:
                                await final_usage
                                raise
                    finally:
                        if on_diagnostics:
                            on_diagnostics(reader.diagnostics())
                turn.adjustments = {
                    **adjustments,
                    "reasoning": reasoning[reasoning_index],
                    "stream": reader.diagnostics(),
                }
                return turn
        await asyncio.sleep(wait)
    raise TurnError("HTTP retry budget exhausted")
