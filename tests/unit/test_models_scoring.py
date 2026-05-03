"""Unit tests for ``wavebench.models``.

Covers:
  * ``is_stealth`` classification
  * ``_model_score`` ranking behavior for provider tier, pricing, recency,
    capability, and context-length components

Scoring has a time-dependent ``recency`` component. Tests construct synthetic
models with ``created`` timestamps well in the past so the recency term
collapses to a constant penalty and doesn't perturb other assertions — except
the recency-specific tests which construct fresh timestamps explicitly.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from wavebench.models import (
    _OPENROUTER_UTILITY,
    _TIER1_PROVIDERS,
    _TIER2_PROVIDERS,
    MODEL_MAPPING,
    TTS_MODEL_MAPPING,
    _model_score,
    is_stealth,
    is_tts_model,
    tts_response_format_for_model,
    tts_voice_for_model,
)

# A ``created`` timestamp 5 years in the past — well past any recency bonus
# so the recency term becomes a known negative constant for any model that
# uses this value. Keeps other assertions independent of wall-clock time.
_STALE = int(time.time() - 5 * 365 * 86400)


def _base_model(**overrides: Any) -> dict:
    """Produce a minimal model dict with all scoring fields present."""
    base = {
        "id": "acme/widget-1",
        "pricing": {"prompt": 0},
        "created": _STALE,
        "supported_parameters": [],
        "context_length": 0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# is_stealth
# ---------------------------------------------------------------------------


def test_is_stealth_true_for_openrouter_namespace() -> None:
    assert is_stealth("openrouter/horizon-beta")
    assert is_stealth("openrouter/mystery-model")


def test_is_stealth_false_for_non_openrouter_namespace() -> None:
    assert not is_stealth("anthropic/claude-opus-4.6")
    assert not is_stealth("google/gemini-3-pro-preview")


@pytest.mark.parametrize("utility_id", sorted(_OPENROUTER_UTILITY))
def test_is_stealth_false_for_utility_models(utility_id: str) -> None:
    assert not is_stealth(utility_id)


# ---------------------------------------------------------------------------
# _model_score — provider tier
# ---------------------------------------------------------------------------


def test_tier1_scores_higher_than_tier2() -> None:
    tier1 = _base_model(id="anthropic/claude")
    tier2 = _base_model(id="qwen/qwen-3")
    assert _model_score(tier1) > _model_score(tier2)


def test_tier2_scores_higher_than_unknown_provider() -> None:
    tier2 = _base_model(id="qwen/qwen-3")
    unknown = _base_model(id="random-vendor/thing")
    assert _model_score(tier2) > _model_score(unknown)


def test_stealth_scores_like_tier1() -> None:
    stealth = _base_model(id="openrouter/horizon-beta")
    tier1 = _base_model(id="anthropic/claude")
    unknown = _base_model(id="random-vendor/thing")
    # Both tier1 and stealth get the +1000 bonus. _model_score reads
    # time.time() internally so the recency penalty drifts by nanoseconds
    # between calls — use approx to accommodate.
    assert _model_score(stealth) == pytest.approx(_model_score(tier1), abs=1e-3)
    assert _model_score(stealth) > _model_score(unknown)


# ---------------------------------------------------------------------------
# _model_score — pricing
# ---------------------------------------------------------------------------


def test_paid_scores_higher_than_free() -> None:
    paid = _base_model(id="anthropic/claude", pricing={"prompt": "0.00002"})
    free = _base_model(id="anthropic/claude", pricing={"prompt": "0"})
    assert _model_score(paid) > _model_score(free)


def test_pricing_bonus_is_capped() -> None:
    # Cap triggers when ``pp * 50_000 >= 300`` — i.e. pp >= 0.006 per token.
    # (The source comment says "caps at ~$6/M" but actually saturates at
    # ~$6000/M — the comment is wrong; we're pinning actual behavior.)
    at_cap = _base_model(id="anthropic/claude", pricing={"prompt": "0.01"})
    above_cap = _base_model(id="anthropic/claude", pricing={"prompt": "0.1"})
    # Both saturate; scores should match modulo time.time() noise.
    assert _model_score(at_cap) == pytest.approx(_model_score(above_cap), abs=1e-3)


def test_pricing_invalid_string_is_treated_as_free() -> None:
    # Non-numeric pricing should not crash; it degrades gracefully to free.
    invalid = _base_model(id="anthropic/claude", pricing={"prompt": "n/a"})
    free = _base_model(id="anthropic/claude", pricing={"prompt": "0"})
    assert _model_score(invalid) == pytest.approx(_model_score(free), abs=1e-3)


# ---------------------------------------------------------------------------
# _model_score — recency
# ---------------------------------------------------------------------------


def test_fresh_model_scores_higher_than_stale_model() -> None:
    now = int(time.time())
    fresh = _base_model(id="anthropic/claude", created=now)
    stale = _base_model(id="anthropic/claude", created=_STALE)
    assert _model_score(fresh) > _model_score(stale)


def test_model_under_10_days_gets_largest_recency_bonus() -> None:
    # A 2-day-old model should outscore a 20-day-old one (both tier1).
    now = int(time.time())
    two_days = _base_model(id="anthropic/claude", created=now - 2 * 86400)
    twenty_days = _base_model(id="anthropic/claude", created=now - 20 * 86400)
    assert _model_score(two_days) > _model_score(twenty_days)


# ---------------------------------------------------------------------------
# _model_score — capability bonuses
# ---------------------------------------------------------------------------


def test_reasoning_capability_adds_score() -> None:
    with_r = _base_model(id="anthropic/claude", supported_parameters=["reasoning"])
    without_r = _base_model(id="anthropic/claude", supported_parameters=[])
    assert _model_score(with_r) - _model_score(without_r) == pytest.approx(80)


def test_tools_capability_adds_score() -> None:
    with_t = _base_model(id="anthropic/claude", supported_parameters=["tools"])
    without_t = _base_model(id="anthropic/claude", supported_parameters=[])
    assert _model_score(with_t) - _model_score(without_t) == pytest.approx(40)


def test_both_capabilities_stack() -> None:
    both = _base_model(id="anthropic/claude", supported_parameters=["reasoning", "tools"])
    base = _base_model(id="anthropic/claude", supported_parameters=[])
    assert _model_score(both) - _model_score(base) == pytest.approx(120)


# ---------------------------------------------------------------------------
# _model_score — context length
# ---------------------------------------------------------------------------


def test_context_length_100k_gets_larger_bonus_than_32k() -> None:
    ctx_100k = _base_model(id="anthropic/claude", context_length=100_000)
    ctx_32k = _base_model(id="anthropic/claude", context_length=32_000)
    ctx_small = _base_model(id="anthropic/claude", context_length=8_000)
    assert _model_score(ctx_100k) > _model_score(ctx_32k) > _model_score(ctx_small)


def test_context_length_below_32k_gets_no_bonus() -> None:
    ctx_8k = _base_model(id="anthropic/claude", context_length=8_000)
    ctx_0 = _base_model(id="anthropic/claude", context_length=0)
    assert _model_score(ctx_8k) == pytest.approx(_model_score(ctx_0), abs=1e-3)


# ---------------------------------------------------------------------------
# TTS model classification
# ---------------------------------------------------------------------------


def test_is_tts_model_detects_tts_slugs() -> None:
    assert is_tts_model("openai/gpt-4o-mini-tts-2025-12-15")
    assert is_tts_model("mistralai/voxtral-mini-tts-2603")
    assert is_tts_model("vendor/speech-synth")


@pytest.mark.parametrize(
    "model_id",
    [
        "google/gemini-3.1-flash-tts-preview",
        "zyphra/zonos-v0.1-transformer",
        "zyphra/zonos-v0.1-hybrid",
        "sesame/csm-1b",
        "canopylabs/orpheus-3b-0.1-ft",
        "hexgrad/kokoro-82m",
    ],
)
def test_is_tts_model_detects_current_openrouter_speech_models(model_id: str) -> None:
    assert is_tts_model(model_id)


def test_is_tts_model_false_for_text_model() -> None:
    assert not is_tts_model("anthropic/claude-opus-4.6")


@pytest.mark.parametrize(
    ("model_id", "expected_voice"),
    [
        ("google/gemini-3.1-flash-tts-preview", "Kore"),
        ("mistralai/voxtral-mini-tts-2603", "en_paul_neutral"),
        ("zyphra/zonos-v0.1-hybrid", "american_female"),
        ("zyphra/zonos-v0.1-transformer", "american_female"),
        ("sesame/csm-1b", "conversational_a"),
        ("canopylabs/orpheus-3b-0.1-ft", "tara"),
        ("hexgrad/kokoro-82m", "af_alloy"),
    ],
)
def test_tts_voice_for_model_maps_default_to_provider_voice(
    model_id: str,
    expected_voice: str,
) -> None:
    assert tts_voice_for_model(model_id, "alloy") == expected_voice


def test_tts_response_format_for_model_maps_default_to_provider_supported_format() -> None:
    assert tts_response_format_for_model("google/gemini-3.1-flash-tts-preview", "mp3") == "pcm"
    assert tts_response_format_for_model("mistralai/voxtral-mini-tts-2603", "mp3") == "mp3"
    assert tts_response_format_for_model("zyphra/zonos-v0.1-hybrid", "mp3") == "mp3"
    assert tts_response_format_for_model("openai/gpt-4o-mini-tts-2025-12-15", "mp3") == "mp3"


def test_tts_response_format_for_model_preserves_explicit_format() -> None:
    assert tts_response_format_for_model("google/gemini-3.1-flash-tts-preview", "wav") == "wav"
    assert tts_response_format_for_model("mistralai/voxtral-mini-tts-2603", "pcm") == "pcm"
    assert tts_response_format_for_model("zyphra/zonos-v0.1-hybrid", "pcm") == "pcm"


def test_tts_voice_for_model_preserves_explicit_voice() -> None:
    assert tts_voice_for_model("google/gemini-3.1-flash-tts-preview", "Puck") == "Puck"
    assert tts_voice_for_model("mistralai/voxtral-mini-tts-2603", "gb_oliver_neutral") == "gb_oliver_neutral"
    assert tts_voice_for_model("openai/gpt-4o-mini-tts-2025-12-15", "nova") == "nova"


# ---------------------------------------------------------------------------
# MODEL_MAPPING sanity
# ---------------------------------------------------------------------------


def test_model_mapping_has_expected_shape() -> None:
    # Not asserting exact contents (they shift over time), just structure.
    assert isinstance(MODEL_MAPPING, dict)
    assert len(MODEL_MAPPING) > 0
    for short_name, full_id in MODEL_MAPPING.items():
        assert isinstance(short_name, str) and short_name
        assert isinstance(full_id, str) and "/" in full_id


def test_tts_model_mapping_has_expected_shape() -> None:
    assert isinstance(TTS_MODEL_MAPPING, dict)
    assert len(TTS_MODEL_MAPPING) > 0
    assert TTS_MODEL_MAPPING["voxtralMiniTts2603"] == "mistralai/voxtral-mini-tts-2603"
    for short_name, full_id in TTS_MODEL_MAPPING.items():
        assert isinstance(short_name, str) and short_name
        assert isinstance(full_id, str) and "/" in full_id
        assert is_tts_model(full_id)


def test_tier_sets_are_disjoint() -> None:
    # A provider shouldn't appear in both tier1 and tier2 — defensive invariant.
    assert not (_TIER1_PROVIDERS & _TIER2_PROVIDERS)
