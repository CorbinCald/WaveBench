from __future__ import annotations

import copy

import pytest

from wavebench.prompt_cache import CachePolicy, affinity
from wavebench.tokens import PromptEstimate, context_usage, count_tokens


def conversation():
    return [
        {"role": "system", "content": "Stable instructions"},
        {"role": "user", "content": "reference data " * 5000},
    ]


def append_tools(messages, count=1):
    messages.append(
        {
            "role": "assistant",
            "content": "",
            "reasoning_details": [{"type": "reasoning.encrypted", "data": "signature"}],
            "tool_calls": [
                {
                    "id": f"id-{len(messages)}-{i}",
                    "type": "function",
                    "function": {"name": "wb", "arguments": '{"command":"ls"}'},
                }
                for i in range(count)
            ],
        }
    )
    messages.extend(
        {"role": "tool", "tool_call_id": c["id"], "content": "result"}
        for c in messages[-1]["tool_calls"]
    )


@pytest.mark.parametrize(
    "model,field",
    [
        ("openai/gpt-5.6-luna", "prompt_cache_breakpoint"),
        ("openai/gpt-6-astra", "prompt_cache_breakpoint"),
        ("anthropic/claude-haiku-4.5", "cache_control"),
    ],
)
def test_cache_prior_boundary_survives_large_parallel_batches_without_mutating_history(
    model, field
):
    policy = CachePolicy(model)
    messages = conversation()
    first_payload, _ = policy.prepare(messages, [], now=0)
    for i in range(6):
        append_tools(messages, 64)
        before = copy.deepcopy(messages)
        payload, record = policy.prepare(messages, [], now=i + 1)
        assert messages == before
        assert payload["session_id"] == first_payload["session_id"]
        assert record["breakpoints"][-1] == len(messages) - 1
        if i:
            assert len(messages) - 66 in record["breakpoints"]
        assert len(record["breakpoints"]) <= 4
        assert payload["messages"][1] == first_payload["messages"][1]
        assert payload["messages"][-65] == messages[-65]
        assert field in payload["messages"][-1]["content"][-1]
    if model.startswith("openai/"):
        assert payload["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
        assert payload["prompt_cache_key"] == payload["session_id"]


def test_google_reuses_checkpoint_until_expiry_and_resets_after_compaction():
    policy = CachePolicy("google/gemini-2.5-flash")
    messages = conversation()
    first, record = policy.prepare(messages, [], now=10)
    assert record["breakpoints"] == [1]
    append_tools(messages)
    second, record = policy.prepare(messages, [], now=309)
    assert record["breakpoints"] == [1]
    assert second["messages"][1] == first["messages"][1]
    assert "cache_control" not in second["messages"][-1]["content"][0]
    _, record = policy.prepare(messages, [], now=310)
    assert record["breakpoints"] == [3]
    key = policy.key
    policy.reset()
    _, record = policy.prepare(conversation(), [], now=311)
    assert record["breakpoints"] == [1]
    assert policy.key == key


def test_google_short_prompts_use_implicit_caching_without_storage_writes():
    _, record = CachePolicy("google/gemini-3-flash-preview").prepare(
        [{"role": "user", "content": "hello"}],
        [],
        now=0,
    )
    assert record["breakpoints"] == []


def test_anthropic_uses_longer_ttl_when_slow_requests_or_queueing_exceed_5m_safety_margin():
    policy = CachePolicy("anthropic/claude-opus-5")
    messages = conversation()
    _, record = policy.prepare(messages, [], now=0)
    assert record["ttl"] == "5m"
    _, record = policy.prepare(messages, [], now=240)
    assert record["ttl"] == "1h"
    _, record = policy.prepare(messages, [], now=241)
    assert record["ttl"] == "1h"


@pytest.mark.parametrize(
    "model",
    [
        "openai/gpt-4.1",
        "openai/gpt-5.5",
        "x-ai/grok-4",
        "deepseek/deepseek-v4",
        "google/gemini-2.0-flash",
        "custom/my-model",
    ],
)
def test_automatic_providers_get_affinity_without_unsupported_parameters(model):
    messages = conversation()
    payload, _ = CachePolicy(model).prepare(messages, [])
    assert payload["messages"] == messages
    assert set(payload) == {"messages", "session_id"} | (
        {"prompt_cache_key"} if model.startswith("openai/") else set()
    )
    assert affinity(model, "a") == affinity(model, "a")
    assert affinity(model, "a") != affinity(model, "b")


def test_keys_isolate_models_and_sessions():
    assert CachePolicy("a").key != CachePolicy("a").key
    assert CachePolicy("a", "run").key != CachePolicy("b", "run").key


def test_token_estimate_counts_text_not_bytes_and_keeps_measured_prefix():
    text = "const greeting = 'Hello world';\n" * 5000 + "こんにちは 👋 <|endoftext|>"
    local = count_tokens(text)
    assert local < len(text.encode()) / 2
    estimate = PromptEstimate()
    assert estimate.estimate(local) == local
    estimate.observe(local, {"prompt_tokens": 50_000})
    assert estimate.bound(local) == 50_000
    assert 50_000 < estimate.bound(local + 100) < 51_000
    estimate.observe(local + 100, {})
    assert estimate.estimate(local) == 50_000


def test_google_cache_write_accounting_is_not_counted_as_twice_the_context():
    usage = {
        "prompt_tokens": 29217,
        "total_tokens": 29220,
        "prompt_tokens_details": {"cache_write_tokens": 14609, "cached_tokens": 14609},
    }
    normalized = context_usage(usage, "google")
    assert normalized["prompt_tokens"] == 14608
    assert usage["prompt_tokens"] == 29217
    assert normalized["total_tokens"] == usage["total_tokens"]
    assert context_usage(usage, "anthropic") == usage
    assert context_usage({}, "google") == {}


def test_compacted_estimate_keeps_vendor_calibration_but_discards_prefix():
    estimate = PromptEstimate()
    estimate.observe(100_000, {"prompt_tokens": 150_000})
    replacement = estimate.after_compaction()
    assert replacement.estimate(1000) == 1500
    assert replacement.bound(1000) == 1650
