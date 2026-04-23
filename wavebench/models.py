"""Default model mapping and ranking algorithm.

Holds the fallback ``MODEL_MAPPING`` used when no persistent selection
exists, and ``_model_score()`` — the heuristic that ranks the OpenRouter
catalog by provider tier, pricing, recency, reasoning/tool capability,
and context length. Also exposes ``is_stealth()`` for classifying cloaked
``openrouter/*`` models.
"""

import time

MODEL_MAPPING: dict[str, str] = {
    "gemini3_0Pro": "google/gemini-3-pro-preview",
    "kimik2_5": "moonshotai/kimi-k2.5",
    "minimax_m2.5": "minimax/minimax-m2.5",
    "glm5": "z-ai/glm-5",
    "claudeOpus4.6": "anthropic/claude-opus-4.6",
}

_TIER1_PROVIDERS: frozenset[str] = frozenset(
    {
        "anthropic",
        "openai",
        "google",
        "deepseek",
        "x-ai",
        "moonshotai",
        "minimax",
    }
)
_TIER2_PROVIDERS: frozenset[str] = frozenset(
    {
        "z-ai",
        "meta-llama",
        "mistralai",
        "bytedance-seed",
        "microsoft",
        "cohere",
        "qwen",
    }
)

_OPENROUTER_UTILITY: frozenset[str] = frozenset(
    {
        "openrouter/auto",
        "openrouter/free",
        "openrouter/bodybuilder",
        "openrouter/cinematika-7b",
    }
)


def is_stealth(model_id: str) -> bool:
    """True for stealth/cloaked models published under the openrouter/ namespace."""
    return model_id.startswith("openrouter/") and model_id not in _OPENROUTER_UTILITY


def _model_score(m: dict) -> float:
    """Score a model for popularity ranking (higher = more prominent)."""
    mid = m.get("id", "")
    provider = mid.split("/")[0] if "/" in mid else ""

    score = 0.0

    # Provider tier (stealth models get T1-equivalent boost)
    if provider in _TIER1_PROVIDERS or is_stealth(mid):
        score += 1000
    elif provider in _TIER2_PROVIDERS:
        score += 500

    # Pricing tier — paid models ranked above free; higher price = more
    # capable frontier model, but capped so price doesn't dominate.
    pricing = m.get("pricing", {})
    try:
        pp = float(pricing.get("prompt") or 0)
    except (TypeError, ValueError):
        pp = 0.0
    if pp > 0:
        score += 200 + min(300, pp * 50_000)  # caps at ~$6/M tokens

    # Recency: bonus for new models, penalty for stale ones
    created = m.get("created") or 0
    age_days = max(0, (time.time() - created) / 86400)
    if age_days <= 10:
        score += 150 * (1 - age_days / 10)
    if age_days <= 30:
        score += 200 - age_days * 2.2
    else:
        score -= (age_days - 30) * 3

    # Capability bonuses
    params = m.get("supported_parameters") or []
    if "reasoning" in params:
        score += 80
    if "tools" in params:
        score += 40

    # Context length bonus (log-scale)
    ctx = m.get("context_length") or 0
    if ctx >= 100_000:
        score += 60
    elif ctx >= 32_000:
        score += 30

    return score
