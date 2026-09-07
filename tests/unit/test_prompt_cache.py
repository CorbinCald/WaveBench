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


def test_anthropic_prior_boundary_survives_large_parallel_batches_without_mutating_history():
    policy = CachePolicy("anthropic/claude-haiku-4.5")
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
        assert "cache_control" in payload["messages"][-1]["content"][-1]


@pytest.mark.parametrize("model", ["openai/gpt-5.6-luna", "openai/gpt-6-astra"])
def test_openai_short_prompt_growing_through_tools_keeps_automatic_caching(model):
    # Regression: the gateway flattens tool content and drops explicit markers.
    # A long user prompt would hide this failure by providing a cacheable anchor.
    messages = [{"role": "user", "content": "Build a website."}]
    policy = CachePolicy(model)
    first, _ = policy.prepare(messages, [])
    for batch in range(3):
        append_tools(messages, 64)
        messages[-1]["content"] = "project source " * 2000
        original = copy.deepcopy(messages)
        payload, record = policy.prepare(messages, [])
        assert payload["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
        assert record["mode"] == "implicit"
        assert record["breakpoints"] == [0]
        assert payload["prompt_cache_key"] == first["prompt_cache_key"] == payload["session_id"]
        assert payload["messages"][0] == first["messages"][0]
        assert payload["messages"][1:] == original[1:]
        assert messages == original
    key = policy.key
    policy.reset()
    payload, record = policy.prepare(messages[:1], [])
    assert policy.key == key
    assert payload["prompt_cache_options"]["mode"] == "implicit"
    assert record["breakpoints"] == [0]


def test_openai_reserves_one_write_slot_for_implicit_caching_across_user_turns():
    policy = CachePolicy("openai/gpt-6-astra")
    messages = conversation()
    user_positions = [1]
    first, _ = policy.prepare(messages, [])
    for _ in range(6):
        append_tools(messages)
        user_positions.append(len(messages))
        messages.append({"role": "user", "content": "Repair the project and resubmit."})
        payload, record = policy.prepare(messages, [])
        assert record["breakpoints"] == sorted({1, *user_positions[-2:]})
        assert len(record["breakpoints"]) <= 3
        assert payload["messages"][1] == first["messages"][1]
        for position in record["breakpoints"]:
            assert payload["messages"][position]["content"][0]["prompt_cache_breakpoint"] == {
                "mode": "explicit"
            }


def test_openai_automatic_mode_does_not_require_an_explicit_text_anchor():
    payload, record = CachePolicy("openai/gpt-6-astra").prepare(
        [{"role": "user", "content": ""}], []
    )
    assert payload["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
    assert record["breakpoints"] == []


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
